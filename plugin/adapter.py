"""XAdapter: polls X classic DMs and answers them as a Hermes gateway platform."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import extra_or_secret, get_scoped_secret, seed_extra_from_env
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import cancel_task, send_chunks, strip_markdown
from plugins.plugin_storage import plugin_data_dir

from .x_api import MAX_AGE, DmEvent, XApiError, XClient, find_xurl, pending_events

if TYPE_CHECKING:
    from hermes_cli.plugins_context import PluginContext

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 300
# GET /2/dm_events allows 15 calls per 15 minutes per user; a poll makes 1 call, up to 5 in a
# burst. Past the limit a poll fails and the next tick retries (same-day re-reads are not billed).
MIN_POLL_SECONDS = 60
MAX_SEEN = 2000
PLATFORM_HINT = (
    "You are Huggy from HuggerHustle, answering an X (Twitter) Direct Message from a member of "
    "the public. X shows plain text only: no markdown, no headings, no tables. Keep replies short "
    "and conversational, a few sentences at most."
)
# A word joiner after "@" breaks Hermes's @file:/@url:/@diff patterns (agent/context_references.py:102).
_REFERENCE_AT = re.compile(r"@(?=[A-Za-z])")


class XAdapter(BasePlatformAdapter):
    # The base reads this class attribute for the per-chat cap, not the registry entry's value.
    MAX_MESSAGE_LENGTH = 10000

    def __init__(self, config: PlatformConfig, **kwargs: Any) -> None:
        super().__init__(config=config, platform=Platform("x"))
        extra = config.extra
        self._user_id = str(extra_or_secret(extra, "user_id", "X_USER_ID") or "").strip()
        try:
            poll = int(
                extra_or_secret(extra, "poll_seconds", "X_POLL_SECONDS", DEFAULT_POLL_SECONDS)
            )
        except (TypeError, ValueError):
            poll = DEFAULT_POLL_SECONDS
        self._poll_seconds = max(poll, MIN_POLL_SECONDS)
        self._api = XClient(find_xurl() or "xurl")
        self._poll_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return "X"

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._user_id:
            self._set_fatal_error("config_missing", "X_USER_ID must be set", retryable=False)
            return False
        if not self._acquire_platform_lock("x", self._user_id, f"X account {self._user_id}"):
            return False
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._mark_connected()
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        await cancel_task(self._poll_task)
        self._poll_task = None
        self._release_platform_lock()
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        async def send_one(chunk: str) -> SendResult:
            try:
                event_id = await self._api.send_dm(chat_id, chunk)
            except XApiError as exc:
                logger.warning("X: send to %s failed (%s): %s", chat_id, type(exc).__name__, exc)
                return SendResult(success=False, error=str(exc), error_kind=exc.error_kind)
            return SendResult(success=True, message_id=event_id)

        chunks = self.truncate_message(self.format_message(content), self.MAX_MESSAGE_LENGTH)
        result = await send_chunks(chunks, send_one)
        assert isinstance(result, SendResult)
        return result

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    async def _record_delivery_obligation(self, *args: Any, **kwargs: Any) -> str | None:
        # No ledger row: its redelivery calls send() again (gateway/delivery_ledger.py:59), so a
        # 402 is re-POSTed and a send that timed out after X accepted it arrives twice.
        return None

    def _send_retry_is_final(self, result: SendResult) -> bool:
        # Never re-send a DM: the base would retry or post a "plain fallback" copy, a second paid
        # call (and on 402 a certain second failure). A lost reply is accepted (plan Risks).
        return not result.success

    def format_message(self, content: str) -> str:
        return strip_markdown(content, keep_link_targets=True)  # X links bare URLs

    def _seen_path(self) -> Path:
        return plugin_data_dir("x") / "seen.json"

    def _load_seen(self) -> tuple[dict[str, None], datetime | None] | None:
        """Seen event ids in insertion order and the cursor (newest event time at the last
        finished poll), or None when there is no usable store (re-seed)."""
        path = self._seen_path()
        try:
            raw = json.loads(path.read_text())
            ids = raw["seen"] if isinstance(raw, dict) else None
            if not isinstance(ids, list):
                raise ValueError(f"no 'seen' list in {path}")
            cursor = _parse_cursor(raw.get("cursor"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError) as exc:
            # Re-seed rather than answer the last 24h again: a double reply is worse than a lost one.
            with contextlib.suppress(OSError):
                os.replace(path, path.with_suffix(".corrupt"))
            logger.error(
                "X: seen-id store unreadable (%s); re-seeding, so DMs since the last good poll stay "
                "unanswered. Check the inbox by hand.",
                exc,
            )
            return None
        return dict.fromkeys(i for i in ids if isinstance(i, str)), cursor

    def _save_seen(self, seen: dict[str, None], cursor: datetime | None) -> None:
        path = self._seen_path()
        tmp = path.with_suffix(".tmp")
        store = {"seen": list(seen)[-MAX_SEEN:], "cursor": cursor.isoformat() if cursor else None}
        with tmp.open("w") as f:
            json.dump(store, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    async def _poll_once(self) -> None:
        if self._message_handler is None:
            # Gateway drain withdrew ingress; marking now would drop the DM for good.
            return
        now = datetime.now(UTC)
        store = self._load_seen()
        cursor = store[1] if store else None
        # Read back only to the cursor: events are billed per event returned.
        floor = now - MAX_AGE if cursor is None else max(cursor, now - MAX_AGE)
        events, complete = await self._api.list_dm_events(floor)
        newest = max([e.created_at for e in events] + ([cursor] if cursor else []), default=None)
        if store is None:
            # First start: mark what is there, answer nothing (cutover from the stopgap).
            self._save_seen(dict.fromkeys(e.id for e in events), newest)
            logger.info("X: first start, seeded %d existing DM events", len(events))
            return
        seen = store[0]
        if events and not any(self._user_id in e.conversation_id.split("-") for e in events):
            # Keep the cursor: once the id is fixed, these DMs must still be answered.
            logger.warning(
                "X: no DM conversation includes X_USER_ID %s; is it wrong?", self._user_id
            )
            return
        if not complete:
            logger.warning("X: DM read stopped before the cursor; older DMs get no reply")
        # One turn per conversation per poll: a second handle_message() on a busy session
        # interrupts the running turn at the default busy_input_mode (gateway/run_busy.py:1022).
        bursts: dict[str, list[DmEvent]] = {}
        for event in pending_events(events, self._user_id, now, since=cursor):
            if event.id not in seen:
                bursts.setdefault(event.conversation_id, []).append(event)
        for burst in bursts.values():
            if self._message_handler is None:
                # Drain landed during the GET or an earlier burst's admission: leave the rest unmarked.
                return
            seen.update(dict.fromkeys(e.id for e in burst))
            # Mark at admission: handle_message returns before the model turn runs, so marking
            # after send() would re-run a paid turn every poll while X rejects the reply.
            self._save_seen(seen, cursor)
            await self.handle_message(self._to_message_event(burst))
        # Only a finished poll moves the cursor: after a drain the next poll re-reads the gap.
        if newest != cursor:
            self._save_seen(seen, newest)

    def _to_message_event(self, burst: list[DmEvent]) -> MessageEvent:
        event = burst[-1]
        text = "\n\n".join(e.text for e in burst)
        source = self.build_source(
            chat_id=event.conversation_id,
            chat_name=event.sender_id,
            chat_type="dm",
            user_id=event.sender_id,
            user_name=event.sender_id,
            message_id=event.id,
        )
        return MessageEvent(
            # Hermes expands @file:/@url: in every inbound turn; a stranger must not read local files.
            text=_REFERENCE_AT.sub("@\u2060", text),
            message_type=MessageType.TEXT,
            source=source,
            user_id=event.sender_id,
            user_name=event.sender_id,
            message_id=event.id,
            raw_message=event,
            timestamp=event.created_at,
            allow_gateway_control=False,  # a stranger's "/cmd" is text, never a gateway command
        )

    async def _poll_safely(self) -> None:
        try:
            await self._poll_once()
        except XApiError as exc:
            logger.warning("X: DM poll failed (%s): %s", type(exc).__name__, exc)
        except Exception:
            logger.exception("X: DM poll crashed")

    async def _poll_loop(self) -> None:
        while True:
            await self._poll_safely()
            await asyncio.sleep(self._poll_seconds)


def _parse_cursor(value: object) -> datetime | None:
    if value is None:
        return None
    cursor = datetime.fromisoformat(value) if isinstance(value, str) else None
    if cursor is None or cursor.tzinfo is None:
        raise ValueError(f"bad cursor {value!r}")
    return cursor


def _user_id_env() -> str:
    return str(get_scoped_secret("X_USER_ID", "") or "").strip()


def _env_enablement() -> dict[str, Any] | None:
    if not _user_id_env():
        return None
    seed: dict[str, Any] = seed_extra_from_env((("X_POLL_SECONDS", "poll_seconds", int),))
    return {"user_id": _user_id_env(), **seed}


def validate_config(config: PlatformConfig) -> bool:
    return bool(extra_or_secret(getattr(config, "extra", None), "user_id", "X_USER_ID"))


def check_requirements() -> bool:
    return find_xurl() is not None


def register(ctx: PluginContext) -> None:
    """Plugin entry point, called by the Hermes plugin system."""
    ctx.register_platform(
        name="x",
        label="X",
        dashboard_description="Answer X (Twitter) Direct Messages as Hermes.",
        adapter_factory=XAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=validate_config,
        required_env=["X_USER_ID"],
        install_hint="No extra packages (stdlib only); needs the xurl CLI with an OAuth2 token",
        env_enablement_fn=_env_enablement,
        allowed_users_env="X_ALLOWED_USERS",
        allow_all_env="X_ALLOW_ALL_USERS",
        max_message_length=XAdapter.MAX_MESSAGE_LENGTH,
        emoji="💬",
        pii_safe=False,
        allow_update_command=False,
        display_tier="minimal",  # no interim, long-running or busy-detail DMs: each is a paid POST
        platform_hint=PLATFORM_HINT,
    )
