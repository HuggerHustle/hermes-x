from pathlib import Path
from unittest.mock import MagicMock

import pytest
from conftest import ME, FakeXurl
from gateway.config import PlatformConfig
from gateway.display_config import resolve_display_setting
from gateway.platform_registry import PlatformEntry, platform_registry

from plugin import register
from plugin.adapter import XAdapter


def _kwargs() -> dict[str, object]:
    ctx = MagicMock()
    register(ctx)
    kwargs: dict[str, object] = ctx.register_platform.call_args.kwargs
    return kwargs


def test_registers_x_with_security_fields() -> None:
    kwargs = _kwargs()
    assert kwargs["name"] == "x"
    assert kwargs["adapter_factory"] is XAdapter
    assert kwargs["allow_update_command"] is False
    assert kwargs["allowed_users_env"] == "X_ALLOWED_USERS"
    assert kwargs["allow_all_env"] == "X_ALLOW_ALL_USERS"
    assert kwargs["max_message_length"] == 10000
    assert kwargs["required_env"] == ["X_USER_ID"]
    assert "Huggy" in str(kwargs["platform_hint"])
    assert "cron_deliver_env_var" not in kwargs
    assert "standalone_sender_fn" not in kwargs


def test_kwargs_build_a_real_platform_entry() -> None:
    # The real register_platform forwards every kwarg to PlatformEntry (plugins_context.py:603).
    PlatformEntry(source="plugin", **_kwargs())  # type: ignore[arg-type]


def test_display_tier_is_minimal() -> None:
    # Each interim/long-running/busy-detail message is one more paid DM to a stranger.
    platform_registry.register(PlatformEntry(source="plugin", **_kwargs()))  # type: ignore[arg-type]
    for key in ("interim_assistant_messages", "long_running_notifications", "busy_ack_detail"):
        assert resolve_display_setting({}, "x", key) is False


def test_env_enablement_and_config_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = _kwargs()
    enable = kwargs["env_enablement_fn"]
    connected = kwargs["is_connected"]
    assert callable(enable) and callable(connected)
    assert enable() is None
    assert connected(PlatformConfig(enabled=True)) is False
    monkeypatch.setenv("X_USER_ID", ME)
    monkeypatch.setenv("X_POLL_SECONDS", "120")
    assert enable() == {"user_id": ME, "poll_seconds": 120}
    assert connected(PlatformConfig(enabled=True)) is True


def test_check_fn_finds_xurl(fake_xurl: FakeXurl, monkeypatch: pytest.MonkeyPatch) -> None:
    check = _kwargs()["check_fn"]
    assert callable(check)
    assert check() is True
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("HOME", "/nonexistent")
    assert check() is False


def test_manifest_is_a_platform_plugin() -> None:
    text = (Path(__file__).parents[1] / "plugin" / "plugin.yaml").read_text()
    assert "kind: platform" in text
    assert "X_USER_ID" in text
