"""apt installs with the .deb files kept on Google Drive, so a new Colab VM does not download them again.

Colab's Ubuntu image deletes downloaded .deb files after installing them, so packages are
fetched with --download-only first, copied to Drive, and only then installed. Next session the
cached files are copied back and apt finds nothing left to download.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .env import DRIVE_ROOT

ARCHIVES = Path("/var/cache/apt/archives")


def _apt(args: list[str]) -> None:
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    r = subprocess.run(["apt-get", "-y", "-qq", *args], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"apt-get {' '.join(args)} failed:\n{r.stderr[-1500:]}")


def install(packages: list[str], cache_name: str = "apt", archives: Path = ARCHIVES,
            drive_root: Path | None = None) -> dict[str, int]:
    cache = (drive_root or DRIVE_ROOT) / "cache" / cache_name
    cache.mkdir(parents=True, exist_ok=True)
    archives.mkdir(parents=True, exist_ok=True)
    restored = 0
    for deb in cache.glob("*.deb"):                     # Drive -> VM
        target = archives / deb.name
        if not target.exists():
            shutil.copy2(deb, target)
            restored += 1
    _apt(["install", "--download-only", *packages])   # downloads only what the cache lacks
    saved = 0
    for deb in archives.glob("*.deb"):                  # VM -> Drive (new files only)
        target = cache / deb.name
        if not target.exists():
            tmp = target.with_suffix(".partial")
            shutil.copy2(deb, tmp)
            tmp.rename(target)
            saved += 1
    _apt(["install", *packages])
    print(f"apt: {len(packages)} packages installed ({restored} files from Drive cache, {saved} newly cached)")
    return {"restored": restored, "saved": saved}
