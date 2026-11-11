import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import CONV, ME, THEM, FakeXurl, dm

from plugin.x_api import (
    CreditsDepleted,
    DmEvent,
    XApiError,
    XClient,
    XRateLimited,
    XurlTimeout,
    pending_events,
)

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
FLOOR = datetime.now(UTC) - timedelta(hours=24)
CREDITS_BODY = json.dumps(
    {
        "title": "CreditsDepleted",
        "detail": "Your enrolled account [1] does not have any credits to fulfill this request.",
        "type": "https://api.twitter.com/2/problems/credits",
    }
)


def ev(event_id: str, sender: str = THEM, minutes_ago: int = 0, conv: str = CONV) -> DmEvent:
    return DmEvent(event_id, conv, sender, "hi", NOW - timedelta(minutes=minutes_ago))


async def test_list_parses_events_and_uses_oauth2(fake_xurl: FakeXurl) -> None:
    fake_xurl.events(dm("1", text="hello"), {"id": "bad"})  # malformed rows are dropped
    events, complete = await XClient(fake_xurl.binary).list_dm_events(FLOOR)
    assert complete
    assert [(e.id, e.text, e.sender_id, e.conversation_id) for e in events] == [
        ("1", "hello", THEM, CONV)
    ]
    argv = fake_xurl.calls()[0]["argv"]
    assert argv[:2] == ["--auth", "oauth2"]
    assert argv[2].startswith("/2/dm_events?")
    assert "event_types=MessageCreate" in argv[2]


async def test_list_with_no_data_is_empty(fake_xurl: FakeXurl) -> None:
    fake_xurl.get(stdout='{"meta": {"result_count": 0}}')
    assert await XClient(fake_xurl.binary).list_dm_events(FLOOR) == ([], True)


async def test_send_posts_json_to_conversation(fake_xurl: FakeXurl) -> None:
    assert await XClient(fake_xurl.binary).send_dm(CONV, 'say "hi"') == "900"
    argv = fake_xurl.calls()[0]["argv"]
    assert argv[:5] == ["--auth", "oauth2", "-X", "POST", f"/2/dm_conversations/{CONV}/messages"]
    assert argv[5] == "-d"
    assert json.loads(argv[6]) == {"text": 'say "hi"'}


async def test_send_rejects_non_conversation_id(fake_xurl: FakeXurl) -> None:
    with pytest.raises(XApiError):
        await XClient(fake_xurl.binary).send_dm("../users/me", "x")
    assert fake_xurl.calls() == []


@pytest.mark.parametrize(
    "body",
    [
        CREDITS_BODY,
        json.dumps({"type": "https://api.x.com/2/problems/credits", "title": "Payment Required"}),
        json.dumps({"errors": [{"title": "CreditsDepleted", "detail": "no credits"}]}),
    ],
)
async def test_402_is_credits_depleted(fake_xurl: FakeXurl, body: str) -> None:
    fake_xurl.get(stdout=body, exit=1, stderr="\x1b[31mError: request failed\x1b[0m")
    with pytest.raises(CreditsDepleted):
        await XClient(fake_xurl.binary).list_dm_events(FLOOR)


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"type": "https://api.x.com/2/problems/dm-rate-limited", "title": "x"}),
        json.dumps({"errors": [{"code": 88, "message": "Rate limit exceeded"}]}),
    ],
)
async def test_rate_limit(fake_xurl: FakeXurl, body: str) -> None:
    fake_xurl.post(stdout=body, exit=1)
    with pytest.raises(XRateLimited) as info:
        await XClient(fake_xurl.binary).send_dm(CONV, "x")
    assert info.value.error_kind == "rate_limited"


async def test_transport_error_uses_stderr(fake_xurl: FakeXurl) -> None:
    fake_xurl.get(exit=1, stderr="\x1b[31mError: HTTP Error: 502 Bad Gateway\x1b[0m\n")
    with pytest.raises(XApiError, match="502 Bad Gateway") as info:
        await XClient(fake_xurl.binary).list_dm_events(FLOOR)
    assert info.value.error_kind == "transient"
    assert "\x1b" not in str(info.value)


async def test_ok_exit_with_errors_and_no_data_is_error(fake_xurl: FakeXurl) -> None:
    fake_xurl.get(stdout=json.dumps({"errors": [{"title": "Not Found"}]}))
    with pytest.raises(XApiError):
        await XClient(fake_xurl.binary).list_dm_events(FLOOR)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_hung_xurl_is_killed_at_timeout(fake_xurl: FakeXurl) -> None:
    fake_xurl.get(stdout="{}", sleep=30)
    start = time.monotonic()
    with pytest.raises(XurlTimeout):
        await XClient(fake_xurl.binary, timeout=1.0).list_dm_events(FLOOR)
    assert time.monotonic() - start < 10  # killed, not waited out
    [pid] = fake_xurl.pids()
    assert not _alive(pid)


async def test_cancellation_kills_the_child(fake_xurl: FakeXurl) -> None:
    fake_xurl.get(stdout="{}", sleep=30)
    task = asyncio.create_task(XClient(fake_xurl.binary).list_dm_events(FLOOR))
    for _ in range(100):  # wait for the child to start
        await asyncio.sleep(0.05)
        if (fake_xurl.root / "pids").exists() and fake_xurl.pids():
            break
    start = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - start < 10  # killed, not waited out
    [pid] = fake_xurl.pids()
    assert not _alive(pid)


async def test_lock_serializes_calls(fake_xurl: FakeXurl) -> None:
    fake_xurl.get(stdout="{}", sleep=0.3)
    client = XClient(fake_xurl.binary)
    await asyncio.gather(client.list_dm_events(FLOOR), client.send_dm(CONV, "x"))
    first, second = sorted(fake_xurl.calls(), key=lambda c: c["start"])
    assert first["end"] <= second["start"]


def test_pending_only_new_inbound_one_to_one_message_create() -> None:
    events = [
        ev("1", minutes_ago=30),
        ev("2", sender=ME, minutes_ago=20),
        ev("3", minutes_ago=10),
        ev("4", minutes_ago=5),
        ev("5", conv="12345"),  # group conversation
    ]
    assert [e.id for e in pending_events(events, ME, NOW)] == ["1", "3", "4"]


def test_pending_never_returns_our_own_messages() -> None:
    assert [
        e.id for e in pending_events([ev("1", minutes_ago=5), ev("2", sender=ME)], ME, NOW)
    ] == ["1"]


def test_pending_keeps_follow_up_older_than_our_reply() -> None:
    events = [ev("A", minutes_ago=3), ev("B", minutes_ago=2), ev("R", sender=ME, minutes_ago=1)]
    assert [e.id for e in pending_events(events, ME, NOW)] == ["A", "B"]


def test_pending_with_wrong_user_id_answers_nothing() -> None:
    # Our replies would otherwise read as inbound and loop on paid turns.
    events = [ev("A", minutes_ago=2), ev("R", sender=ME, minutes_ago=1)]
    assert pending_events(events, "999", NOW) == []


async def test_xurl_config_env_is_scrubbed(
    fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIENT_ID", "other-app")
    await XClient(fake_xurl.binary).list_dm_events(FLOOR)
    assert fake_xurl.calls()[0]["client_id_env"] is False


def test_pending_drops_events_older_than_24h() -> None:
    events = [ev("1", minutes_ago=25 * 60), ev("2", minutes_ago=60)]
    assert [e.id for e in pending_events(events, ME, NOW)] == ["2"]


def test_pending_orders_by_time_across_conversations() -> None:
    other = f"7-{ME}"
    events = [ev("3", minutes_ago=1), ev("9", sender="7", minutes_ago=2, conv=other)]
    assert [e.id for e in pending_events(events, ME, NOW)] == ["9", "3"]


def test_list_skips_non_message_create() -> None:
    from plugin.x_api import parse_dm_events

    body: dict[str, object] = {"data": [dm("1", event_type="ParticipantsJoin"), dm("2")]}
    assert [e.id for e in parse_dm_events(body)] == ["2"]


def test_list_skips_timestamps_without_offset() -> None:
    # A naive time would make every poll raise in pending_events and stall all DMs.
    from plugin.x_api import parse_dm_events

    body: dict[str, object] = {
        "data": [
            dm("1", created_at="2026-11-11T12:00:00"),
            dm("2", created_at="2022-08-12T17:33:34.000Z"),
        ]
    }
    assert [e.id for e in parse_dm_events(body)] == ["2"]
    assert pending_events(parse_dm_events(body), ME, NOW, max_age=timedelta(days=9999))


def _rows(n: int) -> list[dict[str, object]]:
    """``n`` events newest first, one minute apart."""
    now = datetime.now(UTC)
    return [dm(str(i), created_at=(now - timedelta(minutes=i)).isoformat()) for i in range(n)]


async def test_list_pages_back_to_the_floor_only(fake_xurl: FakeXurl) -> None:
    fake_xurl.events(*_rows(45))
    client = XClient(fake_xurl.binary)
    floor = datetime.now(UTC) - timedelta(minutes=10)
    events, complete = await client.list_dm_events(floor)
    assert complete and len(events) == 20  # page 1 already reaches past the floor
    assert len(fake_xurl.calls()) == 1
    events, complete = await client.list_dm_events(datetime.now(UTC) - timedelta(hours=24))
    assert complete and [e.id for e in events] == [str(i) for i in range(45)]
    assert ["pagination_token=tok20" in c["argv"][2] for c in fake_xurl.calls()[1:]] == [
        False,
        True,
        False,
    ]


async def test_list_stops_on_a_strange_next_token(fake_xurl: FakeXurl) -> None:
    body = {"data": [dm("1")], "meta": {"next_token": "../users/me"}}
    fake_xurl.get(stdout=json.dumps(body))
    events, complete = await XClient(fake_xurl.binary).list_dm_events(FLOOR)
    assert [e.id for e in events] == ["1"] and complete is False
    assert len(fake_xurl.calls()) == 1


def test_pending_drops_events_older_than_since() -> None:
    events = [ev("old", minutes_ago=5), ev("new", minutes_ago=1)]
    since = NOW - timedelta(minutes=3)
    assert [e.id for e in pending_events(events, ME, NOW, since=since)] == ["new"]
