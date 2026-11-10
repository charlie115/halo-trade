"""scripts/ops.py against a real database file."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def ops(command: str, db_path: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "HALO_DB_PATH"}
    if db_path is not None:
        env["HALO_DB_PATH"] = db_path
    return subprocess.run([sys.executable, str(REPO / "scripts" / "ops.py"), command], capture_output=True, text=True, env=env, timeout=30)


def test_ops_pause_and_resume_drive_the_real_kill_switch(env):
    assert ops("status", env.db_path).stdout.strip() == "kill_switch=off"
    paused = ops("pause", env.db_path)
    assert paused.returncode == 0, paused.stderr
    assert paused.stdout.strip() == "kill_switch=on"
    assert env.svc.kill_switch() is True
    assert ops("status", env.db_path).stdout.strip() == "kill_switch=on"
    resumed = ops("resume", env.db_path)
    assert resumed.stdout.strip() == "kill_switch=off"
    assert env.svc.kill_switch() is False


def test_ops_refuses_a_missing_database_instead_of_creating_one(tmp_path):
    missing = tmp_path / "typo.sqlite"
    result = ops("pause", str(missing))
    assert result.returncode == 1
    assert not missing.exists()


def test_ops_needs_a_database_path():
    assert ops("status", None).returncode == 2
