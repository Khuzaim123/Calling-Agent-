"""Directory layout and process environment.

Persistent (Google Drive, survives runtime resets):
  $DRIVE_ROOT/models, datasets, results, logs, backups/pg, secrets, cache/{pip,hf,llama.cpp,node,n8n,cloudflared}
Ephemeral (VM local disk, lost on reset, fast):
  /content/runtime/{code, pgdata, run, n8n_home, llama, node, n8n_pkg, tmp}

PostgreSQL data lives on local disk on purpose: Google Drive's FUSE mount does
not provide the permissions/fsync semantics PostgreSQL requires. Durability
comes from pg_dump backups to Drive (ops.pg).
"""
from __future__ import annotations

import glob
import os
import shutil
import site
import sys
from pathlib import Path

DRIVE_ROOT = Path(os.environ.get("VOICE_AGENT_DRIVE", "/content/drive/MyDrive/voice-agent"))
RUNTIME = Path(os.environ.get("VOICE_AGENT_RUNTIME", "/content/runtime"))

PERSISTENT_DIRS = ["models", "datasets", "results", "logs", "backups/pg", "secrets",
                   "cache/pip", "cache/hf", "cache/llama.cpp", "cache/node", "cache/n8n", "cache/cloudflared"]
EPHEMERAL_DIRS = ["code", "pgdata", "run", "n8n_home", "llama", "node", "n8n_pkg", "tmp", "logs"]


def ensure_layout() -> None:
    if not DRIVE_ROOT.parent.exists():
        raise RuntimeError(f"{DRIVE_ROOT.parent} missing: mount Google Drive first")
    for d in PERSISTENT_DIRS:
        (DRIVE_ROOT / d).mkdir(parents=True, exist_ok=True)
    for d in EPHEMERAL_DIRS:
        (RUNTIME / d).mkdir(parents=True, exist_ok=True)


def code_dir() -> Path:
    return RUNTIME / "code"


def nvidia_lib_dirs() -> list[str]:
    """CUDA libraries shipped as pip wheels (nvidia-cublas-cu12, nvidia-cudnn-cu12 ...).

    CTranslate2 dlopen()s cuBLAS/cuDNN by soname; LD_LIBRARY_PATH must contain these
    directories *before* the process starts, so we pass it to child processes.
    """
    roots = site.getsitepackages() + [site.getusersitepackages()]
    dirs = []
    for r in roots:
        dirs += glob.glob(os.path.join(r, "nvidia", "*", "lib"))
    return sorted(set(dirs))


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["VOICE_AGENT_ROOT"] = str(DRIVE_ROOT)
    env["HF_HOME"] = str(DRIVE_ROOT / "cache" / "hf")
    env["PIP_CACHE_DIR"] = str(DRIVE_ROOT / "cache" / "pip")
    env["PYTHONPATH"] = str(code_dir()) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    libs = nvidia_lib_dirs()
    if libs:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(libs + [env.get("LD_LIBRARY_PATH", "")]).rstrip(os.pathsep)
    node_bin = RUNTIME / "node" / "bin"
    n8n_bin = RUNTIME / "n8n_pkg" / "bin"
    env["PATH"] = os.pathsep.join([str(node_bin), str(n8n_bin), env.get("PATH", "")])
    if extra:
        env.update(extra)
    return env


def disk_report() -> dict[str, dict[str, float]]:
    out = {}
    for label, p in (("vm_local", "/content"), ("drive", str(DRIVE_ROOT))):
        if os.path.exists(p):
            u = shutil.disk_usage(p)
            out[label] = {"total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1)}
    return out


def python_ok_for_kokoro() -> bool:
    return sys.version_info < (3, 13)
