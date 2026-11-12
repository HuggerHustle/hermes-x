import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agent.context_references import parse_context_references
from conftest import CONV, ME, THEM, FakeXurl, dm
from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.status import _get_scope_lock_path

from plugin.adapter import XAdapter


def _capture(adapter: XAdapter, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    handler = AsyncMock()
    monkeypatch.setattr(adapter, "handle_message", handler)
    return handler


def _events(handler: AsyncMock) -> list[MessageEvent]:
    return [call.args[0] for call in handler.call_args_list]


async def test_first_start_seeds_and_answers_nothing(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _capture(adapter, monkeypatch)
    fake_xurl.events(dm("1"))
    await adapter._poll_once()
    assert handler.call_count == 0
    fake_xurl.events(dm("2"), dm("1"))
    await adapter._poll_once()
    assert [e.message_id for e in _events(handler)] == ["2"]


async def test_events_are_plain_text_dm_sessions(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()  # seed on an empty inbox
    fake_xurl.events(dm("2", text="/model gpt-x"))
    await adapter._poll_once()
    events = _events(handler)
    assert [e.text for e in events] == ["/model gpt-x"]
    for event in events:
        assert event.allow_gateway_control is False
        assert event.is_command() is False
        assert event.get_command() is None
        assert event.internal is False
        assert event.source.chat_id == CONV
        assert event.source.chat_type == "dm"
        assert event.source.user_id == THEM
        assert adapter._event_session_key(event).endswith(f"x:dm:{CONV}")


async def test_burst_in_one_poll_is_one_turn_per_conversation(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A second handle_message() on a busy session interrupts the running turn (run_busy.py:1022).
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()
    other = f"{ME}-777"
    fake_xurl.events(
        dm("2", text="hi"),
        dm("3", text="/model x", sender="777", conv=other),
        dm("4", text="you there?"),
    )
    await adapter._poll_once()
    events = sorted(_events(handler), key=lambda e: e.source.chat_id)
    assert [(e.source.chat_id, e.text, e.message_id) for e in events] == sorted(
        [(CONV, "hi\n\nyou there?", "4"), (other, "/model x", "3")]
    )
    assert all(e.allow_gateway_control is False for e in events)
    assert {"2", "3", "4"} <= set(json.loads(adapter._seen_path().read_text())["seen"])


async def test_mark_is_written_before_handle_message(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    await adapter._poll_once()
    fake_xurl.events(dm("2"))
    seen_at_admission: list[bool] = []

    async def handler(event: MessageEvent) -> None:
        seen_at_admission.append("2" in json.loads(adapter._seen_path().read_text())["seen"])

    monkeypatch.setattr(adapter, "handle_message", handler)
    await adapter._poll_once()
    assert seen_at_admission == [True]


async def test_restart_does_not_reply_twice(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    await adapter._poll_once()
    fake_xurl.events(dm("2"))
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()
    restarted = XAdapter(PlatformConfig(enabled=True, extra={"user_id": ME}))
    restarted.set_message_handler(AsyncMock())
    handler2 = _capture(restarted, monkeypatch)
    await restarted._poll_once()
    assert handler.call_count == 1
    assert handler2.call_count == 0


async def test_own_and_old_messages_are_ignored(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    fake_xurl.events(dm("2", sender=ME), dm("3", created_at=old), dm("4", conv="999"))
    await adapter._poll_once()
    assert handler.call_count == 0


async def test_402_on_poll_logs_and_survives(
    adapter: XAdapter, fake_xurl: FakeXurl, caplog: pytest.LogCaptureFixture
) -> None:
    fake_xurl.get(stdout=json.dumps({"title": "CreditsDepleted"}), exit=1)
    with caplog.at_level(logging.WARNING):
        await adapter._poll_safely()
    assert "credits" in caplog.text.lower()


async def test_send_targets_conversation_and_strips_markdown(
    adapter: XAdapter, fake_xurl: FakeXurl
) -> None:
    result = await adapter.send(chat_id=CONV, content="**Hi** see [site](https://huggerhustle.com)")
    assert result.success is True
    assert result.message_id == "900"
    argv = fake_xurl.calls()[0]["argv"]
    assert argv[4] == f"/2/dm_conversations/{CONV}/messages"
    text = json.loads(argv[-1])["text"]
    assert "**" not in text
    assert "https://huggerhustle.com" in text


async def test_402_on_send_is_final_and_not_retried(adapter: XAdapter, fake_xurl: FakeXurl) -> None:
    fake_xurl.post(stdout=json.dumps({"title": "CreditsDepleted"}), exit=1)
    result = await adapter._send_with_retry(chat_id=CONV, content="hello")
    assert result.success is False
    assert result.retryable is False
    assert result.error_kind == "unknown"
    assert len(fake_xurl.calls()) == 1  # no inline retry, no plain-text fallback


async def test_connect_starts_polling_and_disconnect_releases_lock(
    adapter: XAdapter, fake_xurl: FakeXurl
) -> None:
    assert await adapter.connect() is True
    assert adapter.is_connected
    for _ in range(100):
        if fake_xurl.calls():
            break
        await asyncio.sleep(0.05)
    assert fake_xurl.calls()
    lock = _get_scope_lock_path("x", ME)
    assert lock.exists()
    await adapter.disconnect()
    assert not adapter.is_connected
    # Same-PID re-acquire always succeeds (gateway/status.py:1555), so check the lock itself.
    assert adapter._platform_lock_identity is None
    assert not lock.exists()


async def test_poll_loop_survives_errors(adapter: XAdapter, fake_xurl: FakeXurl) -> None:
    fake_xurl.get(stdout=json.dumps({"title": "CreditsDepleted"}), exit=1)
    assert await adapter.connect() is True
    adapter._poll_seconds = 0
    for _ in range(200):
        if len(fake_xurl.calls()) >= 2:
            break
        await asyncio.sleep(0.05)
    assert len(fake_xurl.calls()) >= 2
    assert adapter._poll_task is not None and not adapter._poll_task.done()
    await adapter.disconnect()


async def test_no_poll_while_ingress_withdrawn(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    await adapter._poll_once()  # seed
    before = adapter._seen_path().read_text()
    fake_xurl.events(dm("7"))
    adapter.set_message_handler(None)  # type: ignore[arg-type]  # what gateway drain does
    await adapter._poll_once()
    assert adapter._seen_path().read_text() == before
    adapter.set_message_handler(AsyncMock())
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()
    assert [e.message_id for e in _events(handler)] == ["7"]


async def test_drain_during_poll_leaves_unadmitted_dms_unmarked(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    await adapter._poll_once()  # seed
    other = f"{ME}-777"
    fake_xurl.events(dm("2"), dm("3", sender="777", conv=other))
    admitted: list[str] = []

    async def handler(event: MessageEvent) -> None:
        admitted.append(str(event.message_id))
        await asyncio.sleep(0)  # admission yields to the loop...
        adapter.set_message_handler(None)  # type: ignore[arg-type]  # ...and drain lands here

    monkeypatch.setattr(adapter, "handle_message", handler)
    await adapter._poll_once()
    seen = set(json.loads(adapter._seen_path().read_text())["seen"])
    assert len(admitted) == 1
    assert admitted[0] in seen
    assert _cursor(adapter) is None
    assert ({"2", "3"} - set(admitted)).isdisjoint(seen)  # answered after the restart


async def test_follow_up_sent_during_reply_is_admitted(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    await adapter._poll_once()  # seed
    now = datetime.now(UTC)
    at = [(now - timedelta(seconds=s)).isoformat() for s in (60, 50, 30)]
    fake_xurl.events(dm("A", created_at=at[0]))
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()
    fake_xurl.events(
        dm("A", created_at=at[0]), dm("B", created_at=at[1]), dm("R", ME, created_at=at[2])
    )
    await adapter._poll_once()
    assert [e.message_id for e in _events(handler)] == ["A", "B"]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        '{"seen": ["1"',
        '["1"]',
        '{"seen": null}',
        '{"seen": [], "cursor": "nope"}',
        '{"seen": [], "cursor": "2026-11-11T12:00:00"}',  # no offset
        '{"seen": [], "cursor": 5}',
    ],
)
async def test_corrupt_seen_store_reseeds_then_answers_new(
    adapter: XAdapter,
    fake_xurl: FakeXurl,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    bad: str,
) -> None:
    await adapter._poll_once()
    adapter._seen_path().write_text(bad)
    fake_xurl.events(dm("2"))
    handler = _capture(adapter, monkeypatch)
    with caplog.at_level(logging.ERROR):
        await adapter._poll_once()
    assert handler.call_count == 0
    assert "unanswered" in caplog.text
    assert adapter._seen_path().with_suffix(".corrupt").read_text() == bad
    fake_xurl.events(dm("3"), dm("2"))
    await adapter._poll_once()
    assert [e.message_id for e in _events(handler)] == ["3"]


async def test_context_references_are_neutralized(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    await adapter._poll_once()
    text = "hi @huggerhustle @file:.xurl/auth.yml @folder:.hermes @url:https://x.y @diff @staged @git:2"
    assert len(parse_context_references(text)) == 6
    fake_xurl.events(dm("2", text=text))
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()
    [event] = _events(handler)
    assert parse_context_references(event.text) == []
    assert "@" in event.text and "huggerhustle" in event.text


async def test_failed_reply_is_not_ledgered(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A ledger row would be re-sent at +30s/+120s and on boot (gateway/delivery_ledger.py:59),
    # bypassing _send_retry_is_final: a 402 re-POSTed, a timed-out send delivered twice.
    import gateway.delivery_ledger as ledger

    recorded: list[Any] = []
    monkeypatch.setattr(ledger, "ledger_enabled", lambda: True)
    monkeypatch.setattr(ledger, "record_obligation", lambda **kw: recorded.append(kw))
    fake_xurl.post(stdout=json.dumps({"title": "CreditsDepleted"}), exit=1)
    event = MessageEvent(text="hi", source=adapter.build_source(chat_id=CONV, chat_type="dm"))
    result, _ = await adapter.send_final_ledgered(event, "k", "reply", {}, reply_to=None)
    assert result.success is False
    assert recorded == []
    assert len(fake_xurl.calls()) == 1


async def test_connect_fails_when_account_lock_held(
    adapter: XAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gateway.status as gateway_status

    monkeypatch.setattr(
        gateway_status,
        "acquire_scoped_lock",
        lambda scope, identity, metadata=None: (False, {"pid": 4242, "profile": "other"}),
    )
    assert await adapter.connect() is False
    assert adapter._fatal_error_code == "x_lock"


async def test_connect_without_user_id_fails(fake_xurl: FakeXurl) -> None:
    adapter = XAdapter(PlatformConfig(enabled=True, extra={}))
    assert await adapter.connect() is False
    assert adapter._fatal_error_code == "config_missing"


def test_poll_seconds_from_env_with_floor(
    fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = PlatformConfig(enabled=True, extra={"user_id": ME})
    assert XAdapter(config)._poll_seconds == 300
    monkeypatch.setenv("X_POLL_SECONDS", "120")
    assert XAdapter(config)._poll_seconds == 120
    monkeypatch.setenv("X_POLL_SECONDS", "5")
    assert XAdapter(config)._poll_seconds == 60


def _at(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


def _burst(n: int, conv: str = CONV, sender: str = THEM, offset: int = 0) -> list[dict[str, Any]]:
    """``n`` DMs, newest first (as X returns them), one second apart."""
    return [
        dm(f"{conv}-{offset + i}", sender=sender, conv=conv, created_at=_at(offset + i))
        for i in range(n)
    ]


def _cursor(adapter: XAdapter) -> str | None:
    value: str | None = json.loads(adapter._seen_path().read_text())["cursor"]
    return value


async def test_idle_poll_after_restart_reads_one_small_page(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_xurl.events(*_burst(25))
    await adapter._poll_once()  # seed reads both pages back to the 24 h line
    assert len(fake_xurl.list_calls()) == 2
    restarted = XAdapter(PlatformConfig(enabled=True, extra={"user_id": ME}))
    restarted.set_message_handler(AsyncMock())
    handler = _capture(restarted, monkeypatch)
    await restarted._poll_once()
    calls = fake_xurl.list_calls()
    assert len(calls) == 3  # the cursor survived the restart: one more call
    assert "max_results=20" in calls[-1]["argv"][2]
    assert handler.call_count == 0


async def test_burst_over_one_page_is_read_and_answered(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = dm("old", created_at=_at(3600))
    fake_xurl.events(old)
    await adapter._poll_once()  # seed: the cursor is at "old", as in production
    burst = _burst(30)
    fake_xurl.events(*burst, old)
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()
    assert len(fake_xurl.list_calls()) == 1 + 2
    [event] = _events(handler)
    assert event.message_id == burst[0]["id"]  # newest
    assert event.text.count("hi") == 30
    assert _cursor(adapter) == datetime.fromisoformat(burst[0]["created_at"]).isoformat()


async def test_drain_keeps_cursor_so_skipped_dm_is_answered_later(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = dm("old", created_at=_at(3600))
    fake_xurl.events(old)
    await adapter._poll_once()  # seed: the cursor is at "old"
    seeded = _cursor(adapter)
    other = f"{ME}-777"
    a1, b2, a3 = (
        dm("a1", created_at=_at(500)),
        dm("b2", sender="777", conv=other, created_at=_at(400)),
        dm("a3", created_at=_at(300)),
    )
    fake_xurl.events(a3, b2, a1, old)

    async def drain_after_first(event: MessageEvent) -> None:
        adapter.set_message_handler(None)  # type: ignore[arg-type]

    monkeypatch.setattr(adapter, "handle_message", drain_after_first)
    await adapter._poll_once()  # conversation A admitted, then drain: B never handed over
    assert _cursor(adapter) == seeded  # not moved past b2
    adapter.set_message_handler(AsyncMock())
    handler = _capture(adapter, monkeypatch)
    fake_xurl.events(*_burst(25, conv=f"{ME}-888", sender="888"), a3, b2, a1, old)
    await adapter._poll_once()
    admitted = {e.message_id for e in _events(handler)}
    assert "b2" in admitted and "a3" not in admitted


async def test_nothing_older_than_the_cursor_is_answered(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = dm("1", created_at=_at(10))
    fake_xurl.events(first)
    await adapter._poll_once()  # seed: cursor at event 1
    handler = _capture(adapter, monkeypatch)
    fake_xurl.events(first, dm("late", created_at=_at(20)))  # shows up after the cursor moved
    await adapter._poll_once()
    assert handler.call_count == 0


async def test_more_than_five_pages_warns_and_answers_what_was_read(
    adapter: XAdapter,
    fake_xurl: FakeXurl,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await adapter._poll_once()  # seed
    burst = _burst(110)
    fake_xurl.events(*burst)
    handler = _capture(adapter, monkeypatch)
    with caplog.at_level(logging.WARNING):
        await adapter._poll_once()
    assert len(fake_xurl.list_calls()) == 1 + 5
    assert "more than 100 DM events" in caplog.text
    assert "older DMs get no reply" in caplog.text
    [event] = _events(handler)
    assert event.text.count("hi") == 100
    assert _cursor(adapter) is not None


async def test_store_without_cursor_is_read_not_reseeded(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter._seen_path().parent.mkdir(parents=True, exist_ok=True)
    adapter._seen_path().write_text('{"seen": ["1"]}')  # written before the cursor existed
    fake_xurl.events(dm("2"), dm("1"))
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()
    assert [e.message_id for e in _events(handler)] == ["2"]


async def test_wrong_user_id_keeps_the_cursor(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    await adapter._poll_once()  # seed
    dms = (dm("3", created_at=_at(1)), dm("2", sender="77", conv=f"77-{ME}", created_at=_at(60)))
    fake_xurl.events(*dms)
    wrong = XAdapter(PlatformConfig(enabled=True, extra={"user_id": "999"}))
    wrong.set_message_handler(AsyncMock())
    await wrong._poll_once()
    assert _cursor(adapter) is None
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()  # id fixed: both DMs are still answered
    assert sorted(str(e.message_id) for e in _events(handler)) == ["2", "3"]


async def test_drain_during_the_read_marks_nothing(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    await adapter._poll_once()  # seed
    before = adapter._seen_path().read_text()
    fake_xurl.events(dm("2"))
    read = adapter._api.list_dm_events

    async def read_then_drain(floor: datetime) -> Any:
        events = await read(floor)
        adapter.set_message_handler(None)  # type: ignore[arg-type]
        return events

    monkeypatch.setattr(adapter._api, "list_dm_events", read_then_drain)
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()
    assert handler.call_count == 0
    assert adapter._seen_path().read_text() == before


async def test_group_chat_is_not_a_wrong_user_id(
    adapter: XAdapter,
    fake_xurl: FakeXurl,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await adapter._poll_once()  # seed
    fake_xurl.events(dm("g2", sender="77", conv="1234567890", created_at=_at(1)))
    handler = _capture(adapter, monkeypatch)
    with caplog.at_level(logging.WARNING):
        await adapter._poll_once()
    assert "X_USER_ID" not in caplog.text
    assert _cursor(adapter) is not None  # the cursor moves past the group message
    assert handler.call_count == 0


async def test_read_error_keeps_the_store(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    from pathlib import Path

    await adapter._poll_once()  # seed
    fake_xurl.events(dm("2"))

    def no_handles(self: Path, *args: Any, **kwargs: Any) -> str:
        raise OSError(errno.EMFILE, "Too many open files")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", no_handles)
        with pytest.raises(OSError):
            await adapter._poll_once()
    assert not adapter._seen_path().with_suffix(".corrupt").exists()
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()  # the next poll answers what waited
    assert [e.message_id for e in _events(handler)] == ["2"]


async def test_backlog_admits_only_what_x_lets_us_answer(
    adapter: XAdapter, fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch
) -> None:
    await adapter._poll_once()  # seed
    waiting = [
        dm(f"d{i}", sender=f"7{i}", conv=f"7{i}-{ME}", created_at=_at(60 - i)) for i in range(7)
    ]
    fake_xurl.events(*reversed(waiting))
    handler = _capture(adapter, monkeypatch)
    await adapter._poll_once()  # 300 s poll: 15 sends / 3 polls = 5 conversations
    assert [e.message_id for e in _events(handler)] == [f"d{i}" for i in range(5)]
    assert _cursor(adapter) is None  # held, so the rest are read again
    await adapter._poll_once()
    assert [e.message_id for e in _events(handler)][5:] == ["d5", "d6"]
    assert _cursor(adapter) is not None
