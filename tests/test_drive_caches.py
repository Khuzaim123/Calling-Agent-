"""Installers are kept on Google Drive so a new Colab VM does not download them again."""
import subprocess

import ops.apt as apt


def test_apt_debs_are_cached_on_drive_and_reused(tmp_path, monkeypatch):
    archives, drive = tmp_path / "archives", tmp_path / "drive"
    calls = []

    def fake_run(cmd, env=None, capture_output=None, text=None):
        calls.append(cmd)
        if "--download-only" in cmd:                      # apt fetches whatever is not present yet
            for name in ("postgresql-16_1.deb", "libpq5_1.deb"):
                if not (archives / name).exists():
                    (archives / name).write_bytes(b"deb")
        else:                                             # Colab's docker-clean hook removes .debs
            for d in archives.glob("*.deb"):
                d.unlink()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(apt.subprocess, "run", fake_run)
    first = apt.install(["postgresql-16"], cache_name="apt-pg", archives=archives, drive_root=drive)
    assert first == {"restored": 0, "saved": 2}
    assert sorted(p.name for p in (drive / "cache" / "apt-pg").glob("*.deb")) == ["libpq5_1.deb", "postgresql-16_1.deb"]

    second = apt.install(["postgresql-16"], cache_name="apt-pg", archives=archives, drive_root=drive)   # new VM
    assert second == {"restored": 2, "saved": 0}
