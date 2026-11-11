import os
import subprocess
import sys
from pathlib import Path

from gateway.platforms.base import BasePlatformAdapter

import plugin


def test_imports_plugin_and_hermes() -> None:
    assert plugin.__name__ == "plugin"
    assert BasePlatformAdapter.__module__ == "gateway.platforms.base"


def test_suite_never_touches_the_real_hermes_home(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
    env["HOME"] = str(tmp_path)
    repo = Path(__file__).parents[1]
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-k", "imports_plugin"],
        cwd=repo / "tests",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert not (tmp_path / ".hermes").exists()
