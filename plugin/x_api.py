"""The only X API calls: list DM events and post a DM reply, both through the xurl CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

LIST_PATH = (
    "/2/dm_events?event_types=MessageCreate"
    "&dm_event.fields=id,text,event_type,dm_conversation_id,created_at,sender_id"
)
MAX_AGE = timedelta(hours=24)
# Reads are billed per event returned: small pages, read only back to the caller's floor.
PAGE_SIZE = 20
MAX_PAGES = 5
_PAGE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")  # it goes into the URL query
# 1:1 conversations are "<id>-<id>"; group conversations are a single id.
_ONE_TO_ONE = re.compile(r"^\d+-\d+$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# xurl reads these from the environment and would override the stored app config.
_XURL_CONFIG_ENV = frozenset(
    {
        "CLIENT_ID",
        "CLIENT_SECRET",
        "API_BASE_URL",
        "TOKEN_URL",
        "AUTH_URL",
        "INFO_URL",
        "REDIRECT_URI",
    }
)


class XApiError(Exception):
    """An xurl call failed. ``error_kind`` is a Hermes ``SEND_ERROR_KINDS`` value."""

    error_kind = "unknown"


class CreditsDepleted(XApiError):
    """HTTP 402: the X account has no API credits. Account-wide, so not a dead-chat kind."""


class XRateLimited(XApiError):
    error_kind = "rate_limited"


class XTransportError(XApiError):
    """Network, auth or non-JSON HTTP error: xurl printed only stderr."""

    error_kind = "transient"


class XurlTimeout(XApiError):
    """xurl hung (e.g. a missing token makes it wait for a browser login) and was killed."""


@dataclass(frozen=True)
class DmEvent:
    id: str
    conversation_id: str
    sender_id: str
    text: str
    created_at: datetime


def find_xurl() -> str | None:
    """xurl on PATH, else ~/.local/bin, where the VM installs it and a service PATH may not look."""
    return shutil.which("xurl") or shutil.which("xurl", path=str(Path.home() / ".local" / "bin"))


def _parse_json(stdout: str) -> dict[str, object] | None:
    text = _ANSI.sub("", stdout)
    start = text.find("{")
    if start < 0:
        return None
    try:
        body = json.loads(text[start:])
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _problems(body: dict[str, object]) -> list[dict[str, object]]:
    """The top-level problem plus each ``errors[]`` entry."""
    errors = body.get("errors")
    nested = [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []
    return [body, *nested]


def _api_error(body: dict[str, object]) -> XApiError:
    message = json.dumps(body)[:500]
    for problem in _problems(body):
        title, kind = str(problem.get("title", "")), str(problem.get("type", ""))
        # 402 shape is undocumented; third-party reports give this title or a /credits type.
        if title.lower() == "creditsdepleted" or "credits" in kind:
            return CreditsDepleted(message)
        if kind.endswith(("rate-limited", "usage-capped")) or problem.get("code") == 88:
            return XRateLimited(message)
    return XApiError(message)


def _str_field(row: dict[str, object], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) else None


def parse_dm_events(body: dict[str, object]) -> list[DmEvent]:
    """MessageCreate events from a ``/2/dm_events`` body; malformed rows are dropped."""
    rows = body.get("data")
    events: list[DmEvent] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("event_type") != "MessageCreate":
            continue
        fields = [_str_field(row, k) for k in ("id", "dm_conversation_id", "sender_id", "text")]
        created = _str_field(row, "created_at")
        event_id, conv, sender, text = fields
        if not (event_id and conv and sender and created) or text is None:
            continue
        try:
            created_at = datetime.fromisoformat(created)
        except ValueError:
            continue
        if created_at.tzinfo is None:  # pending_events compares it with an aware "now"
            continue
        events.append(DmEvent(event_id, conv, sender, text, created_at))
    return events


def pending_events(
    events: list[DmEvent],
    me: str,
    now: datetime,
    max_age: timedelta = MAX_AGE,
    since: datetime | None = None,
) -> list[DmEvent]:
    """Inbound events in 1:1 conversations that include ``me``, newer than ``max_age`` and not
    older than ``since``, oldest first. Dedupe is the caller's seen-id store: a follow-up sent
    while we were replying is older than our reply and must still count. A wrong ``me`` matches
    no conversation, so our own replies are never read back as inbound."""
    pending = [
        e
        for e in events
        if _ONE_TO_ONE.match(e.conversation_id)
        and me in e.conversation_id.split("-")
        and e.sender_id != me
        and now - e.created_at <= max_age
        and (since is None or e.created_at >= since)
    ]
    return sorted(pending, key=lambda e: e.created_at)


class XClient:
    """Runs xurl. One lock around every call, so a poll and a send never refresh the OAuth2 token
    at once: xurl rewrites ~/.xurl/auth.yml without a lock and X rotates refresh tokens."""

    def __init__(self, binary: str, timeout: float = 60.0) -> None:
        self._binary = binary
        self._timeout = timeout
        self._lock = asyncio.Lock()

    async def _run(self, *args: str) -> dict[str, object]:
        env = {k: v for k, v in os.environ.items() if k not in _XURL_CONFIG_ENV}
        # --auth oauth2: otherwise a failed refresh silently falls back to app auth and a vague 403.
        argv = [self._binary, "--auth", "oauth2", *args]
        async with self._lock:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                async with asyncio.timeout(self._timeout):
                    out, err = await proc.communicate()
            except BaseException as exc:  # timeout or cancellation: never leave xurl running
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await asyncio.shield(proc.wait())
                if isinstance(exc, TimeoutError):
                    raise XurlTimeout(f"xurl timed out after {self._timeout}s") from None
                raise
        body = _parse_json(out.decode(errors="replace"))
        if body is None:
            stderr = _ANSI.sub("", err.decode(errors="replace")).strip()
            raise XTransportError(stderr or f"xurl exited {proc.returncode} with no JSON")
        if proc.returncode != 0 or ("errors" in body and "data" not in body):
            raise _api_error(body)
        return body

    async def list_dm_events(self, floor: datetime) -> tuple[list[DmEvent], bool]:
        """Events newest first, paged back until one is older than ``floor``. The flag is False
        when ``MAX_PAGES`` ran out first, so older events since ``floor`` were never read."""
        events: list[DmEvent] = []
        token = ""
        for _ in range(MAX_PAGES):
            query = f"&max_results={PAGE_SIZE}" + (f"&pagination_token={token}" if token else "")
            body = await self._run(LIST_PATH + query)
            page = parse_dm_events(body)
            events.extend(page)
            if any(e.created_at < floor for e in page):
                return events, True
            meta = body.get("meta")
            token = str(meta.get("next_token") or "") if isinstance(meta, dict) else ""
            if not token:
                return events, True
            if not _PAGE_TOKEN.match(token):
                logger.warning("X: unexpected next_token %r; reading no further", token[:40])
                return events, False
        logger.warning("X: more than %d DM events since the last poll", MAX_PAGES * PAGE_SIZE)
        return events, False

    async def send_dm(self, conversation_id: str, text: str) -> str:
        """Post ``text`` to a 1:1 conversation; returns the new DM event id."""
        if not _ONE_TO_ONE.match(conversation_id):  # it goes into the URL path
            raise XApiError(f"not a 1:1 conversation id: {conversation_id!r}")
        body = await self._run(
            "-X",
            "POST",
            f"/2/dm_conversations/{conversation_id}/messages",
            "-d",
            json.dumps({"text": text}),
        )
        data = body.get("data")
        event_id = data.get("dm_event_id") if isinstance(data, dict) else None
        return event_id if isinstance(event_id, str) else ""
