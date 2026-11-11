import atexit
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

# Importing gateway runs load_config -> ensure_hermes_home (hermes_cli/config.py:2238), so the
# sandbox home must exist before the first Hermes import, not only inside a fixture.
_IMPORT_HOME = tempfile.mkdtemp(prefix="hermes-test-")
atexit.register(shutil.rmtree, _IMPORT_HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _IMPORT_HOME

import pytest  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402

from plugin.adapter import XAdapter  # noqa: E402

ME = "2112799656950259712"
THEM = "42"
CONV = f"{THEM}-{ME}"


class FakeXurl:
    """Drives tests/fake_xurl.py, installed as `xurl` on PATH."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.binary = str(root / "bin" / "xurl")
        self.get(stdout="{}")
        self.post(stdout=json.dumps({"data": {"dm_conversation_id": CONV, "dm_event_id": "900"}}))

    def _write(self, name: str, stdout: str, exit: int, sleep: float, stderr: str) -> None:
        spec = {"stdout": stdout, "exit": exit, "sleep": sleep, "stderr": stderr}
        (self.root / name).write_text(json.dumps(spec))

    def get(self, stdout: str = "", exit: int = 0, sleep: float = 0, stderr: str = "") -> None:
        self._write("get.json", stdout, exit, sleep, stderr)

    def post(self, stdout: str = "", exit: int = 0, sleep: float = 0, stderr: str = "") -> None:
        self._write("post.json", stdout, exit, sleep, stderr)

    def events(self, *events: dict[str, Any]) -> None:
        """Serve ``events`` (newest first, as X does) in pages."""
        spec = {"events": list(events), "exit": 0, "sleep": 0, "stderr": ""}
        (self.root / "get.json").write_text(json.dumps(spec))

    def list_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls() if "POST" not in c["argv"]]

    def calls(self) -> list[dict[str, Any]]:
        log = self.root / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def pids(self) -> list[int]:
        return [int(p.name) for p in (self.root / "pids").iterdir()]


@pytest.fixture(autouse=True)
def _hermes_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    for key in ("X_USER_ID", "X_POLL_SECONDS", "X_ALLOWED_USERS", "X_ALLOW_ALL_USERS"):
        monkeypatch.delenv(key, raising=False)
    # Platform("x") only resolves once "x" is registered (gateway/config.py:244-266).
    platform_registry.register(
        PlatformEntry(name="x", label="X", adapter_factory=XAdapter, check_fn=lambda: True)
    )


@pytest.fixture
def fake_xurl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeXurl:
    root = tmp_path / "xurl"
    (root / "bin").mkdir(parents=True)
    script = Path(__file__).with_name("fake_xurl.py")
    wrapper = root / "bin" / "xurl"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_XURL_DIR", str(root))
    monkeypatch.setenv("PATH", f"{root / 'bin'}:/usr/bin:/bin")
    return FakeXurl(root)


@pytest.fixture
def adapter(fake_xurl: FakeXurl) -> Iterator[XAdapter]:
    adapter = XAdapter(PlatformConfig(enabled=True, extra={"user_id": ME}))
    adapter.set_message_handler(AsyncMock())  # the gateway installs one; drain sets None
    yield adapter


def dm(
    event_id: str,
    sender: str = THEM,
    text: str = "hi",
    conv: str = CONV,
    created_at: str = "",
    event_type: str = "MessageCreate",
) -> dict[str, Any]:
    from datetime import UTC, datetime

    return {
        "id": event_id,
        "event_type": event_type,
        "text": text,
        "sender_id": sender,
        "dm_conversation_id": conv,
        "created_at": created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
