"""Tests for disk hygiene (agent.cleanup)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from agent import cleanup


def _write(path: Path, size: int = 16) -> Path:
    path.write_bytes(b"x" * size)
    return path


def test_sweep_downloads_removes_old_leftovers(tmp_path):
    old = _write(tmp_path / "abc.f299.mp4", 1024)
    partial = _write(tmp_path / "abc.temp.mp4", 512)
    fresh = _write(tmp_path / "live.f140.m4a", 256)
    stale = time.time() - 7200
    os.utime(old, (stale, stale))
    os.utime(partial, (stale, stale))

    stats = cleanup.sweep_downloads(tmp_path, grace_seconds=3600)

    assert stats["deleted"] == 2
    assert stats["bytes"] == 1024 + 512
    assert stats["kept"] == 1
    assert not old.exists()
    assert not partial.exists()
    assert fresh.exists()


def test_sweep_downloads_ignores_missing_dir(tmp_path):
    stats = cleanup.sweep_downloads(tmp_path / "nope")
    assert stats == {"deleted": 0, "bytes": 0, "kept": 0}


def test_sweep_downloads_grace_zero_removes_everything(tmp_path):
    a = _write(tmp_path / "a.mp4")
    b = _write(tmp_path / "b.m4a")

    stats = cleanup.sweep_downloads(tmp_path, grace_seconds=0)

    assert stats["deleted"] == 2
    assert not a.exists() and not b.exists()


def test_autonomous_downloads_dir_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_SOURCE_DIR", str(tmp_path / "sources"))
    assert cleanup.autonomous_downloads_dir() == tmp_path / "sources" / "downloads"


def test_free_bytes_reports_number(tmp_path):
    assert cleanup.free_bytes(tmp_path) > 0


def test_free_bytes_missing_path_returns_minus_one(tmp_path):
    assert cleanup.free_bytes(tmp_path / "does-not-exist") == -1
