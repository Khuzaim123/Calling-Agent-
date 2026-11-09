"""pg.backup() with pg_dump simulated (PostgreSQL itself only runs in Colab).

Regression: pg_dump ran as `postgres` with -f into a root-owned directory and failed with
permission denied (2026-11-08). It must now stream to stdout and the caller writes the file.
"""
import subprocess

import pytest

import ops.pg as pg


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "DRIVE_ROOT", tmp_path / "drive")
    monkeypatch.setattr(pg, "RUNTIME", tmp_path / "rt")
    (tmp_path / "rt" / "tmp").mkdir(parents=True)
    return tmp_path


def test_backup_streams_dump_and_writes_to_drive(dirs, monkeypatch):
    calls = []

    def fake_run(cmd, stdout=None, stderr=None, **kw):
        calls.append(cmd)
        assert "-f" not in cmd, "pg_dump must not write files itself (runs as postgres)"
        stdout.write(b"PGDMP fake dump of " + cmd[-1].encode())
        return subprocess.CompletedProcess(cmd, 0, None, b"")

    monkeypatch.setattr(pg.subprocess, "run", fake_run)
    out = pg.backup()
    assert [p.parent.name for p in out] == list(pg.DATABASES)
    assert out[0].read_bytes().startswith(b"PGDMP")
    assert not list((dirs / "rt" / "tmp").iterdir())            # temp files cleaned up


def test_backup_failure_reports_pg_dump_error(dirs, monkeypatch):
    def fake_run(cmd, stdout=None, stderr=None, **kw):
        return subprocess.CompletedProcess(cmd, 1, None, b"pg_dump: error: connection refused")

    monkeypatch.setattr(pg.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="connection refused"):
        pg.backup()
