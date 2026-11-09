"""Node.js + n8n for Colab (n8n uses PostgreSQL, never SQLite here).

n8n is NOT exposed to the internet. The editor is opened through Colab's
authenticated port proxy; the voice engine calls n8n webhooks on 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from .env import DRIVE_ROOT, RUNTIME, child_env, code_dir
from .services import Service, http_ok

NODE_VERSION = os.environ.get("NODE_VERSION", "v24.22.1")   # 2026-10-20; n8n 2.x needs Node >= 24
N8N_VERSION = os.environ.get("N8N_VERSION", "2.45.3")       # 2026-10-23
PORT = 5678


def ensure_node() -> Path:
    node = RUNTIME / "node" / "bin" / "node"
    if node.exists():
        return node
    name = f"node-{NODE_VERSION}-linux-x64.tar.xz"
    cache = DRIVE_ROOT / "cache" / "node" / name
    if not cache.exists():
        url = f"https://nodejs.org/dist/{NODE_VERSION}/{name}"
        print("downloading", url)
        tmp = cache.with_suffix(".partial")
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(cache)
    dest = RUNTIME / "node"
    shutil.rmtree(dest, ignore_errors=True)
    with tarfile.open(cache) as tf:
        tf.extractall(RUNTIME / "tmp")
    shutil.move(str(RUNTIME / "tmp" / name.replace(".tar.xz", "")), str(dest))
    out = subprocess.run([str(node), "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if out != NODE_VERSION:
        raise RuntimeError(f"node version mismatch: {out}")
    return node


def ensure_n8n() -> Path:
    exe = RUNTIME / "n8n_pkg" / "bin" / "n8n"
    if exe.exists():
        return exe
    cache = DRIVE_ROOT / "cache" / "n8n" / f"n8n-{N8N_VERSION}-node{NODE_VERSION}.tar"
    prefix = RUNTIME / "n8n_pkg"
    if cache.exists():
        print("restoring n8n from cache", cache.name)
        with tarfile.open(cache) as tf:
            tf.extractall(prefix)
    else:
        env = dict(os.environ, PATH=f"{RUNTIME / 'node' / 'bin'}:{os.environ['PATH']}")
        print(f"npm install n8n@{N8N_VERSION} (one-time, several minutes) ...")
        subprocess.run([str(RUNTIME / "node" / "bin" / "npm"), "install", "-g", "--prefix", str(prefix),
                        "--no-audit", "--no-fund", "--loglevel=error", f"n8n@{N8N_VERSION}"], check=True, env=env)
        with tarfile.open(cache.with_suffix(".partial"), "w") as tf:
            for child in prefix.iterdir():
                tf.add(child, arcname=child.name)
        cache.with_suffix(".partial").rename(cache)
    if not exe.exists():
        raise RuntimeError("n8n executable missing after install")
    return exe


WORKFLOWS_DIR = code_dir() / "n8n" / "workflows"
# credential ids referenced by the generated workflows (tools/build_workflows.py)
CREDENTIALS = [
    ("vaPgApp000000001", "VA Postgres (va_app)", "postgres",
     lambda e: {"host": "127.0.0.1", "port": 5432, "database": "voiceagent", "user": "va_app",
                "password": e["PG_APP_PASSWORD"], "ssl": "disable", "allowUnauthorizedCerts": False,
                "maxConnections": 10}),
    ("vaHmacSecret0001", "VA webhook HMAC", "crypto", lambda e: {"hmacSecret": e["N8N_WEBHOOK_HMAC_SECRET"]}),
    ("vaLlmApiKey00001", "VA LLM key", "httpHeaderAuth",
     lambda e: {"name": "Authorization", "value": "Bearer " + e["LLM_API_KEY"]}),
    ("vaCallsToken0001", "VA voice engine calls token", "httpHeaderAuth",
     lambda e: {"name": "Authorization", "value": "Bearer " + e["VOICE_ENGINE_CALLS_TOKEN"]}),
]


def _cli(*args: str) -> str:
    r = subprocess.run([str(RUNTIME / "n8n_pkg" / "bin" / "n8n"), *args], capture_output=True, text=True,
                       env=child_env(_env()), timeout=600)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise RuntimeError(f"n8n {args[0]} failed:\n{out[-1500:]}")
    return out


def install_workflows() -> None:
    """Import credentials + workflows and publish them. Run while n8n is STOPPED (the CLI writes to its DB).

    Idempotent: ids are fixed, so re-running updates in place. Secrets go through a temp file that is
    deleted immediately; n8n encrypts them with N8N_ENCRYPTION_KEY on import.
    """
    creds = [{"id": i, "name": n, "type": t, "data": f(os.environ)} for i, n, t, f in CREDENTIALS]
    tmp = RUNTIME / "tmp" / "va-credentials.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(creds, fh)
        _cli("import:credentials", f"--input={tmp}")
    finally:
        tmp.unlink(missing_ok=True)
    print(f"credentials imported: {', '.join(n for _, n, _, _ in CREDENTIALS)}")
    _cli("import:workflow", "--separate", f"--input={WORKFLOWS_DIR}")
    for f in sorted(WORKFLOWS_DIR.glob("*.json")):
        wf = json.loads(f.read_text(encoding="utf-8"))
        _cli("publish:workflow", f"--id={wf['id']}")
        print(f"workflow published: {wf['name']}")


def _env() -> dict[str, str]:
    return {
        "DB_TYPE": "postgresdb",
        "DB_POSTGRESDB_HOST": "127.0.0.1",
        "DB_POSTGRESDB_PORT": "5432",
        "DB_POSTGRESDB_DATABASE": "n8n",
        "DB_POSTGRESDB_USER": "n8n_sys",
        "DB_POSTGRESDB_PASSWORD": os.environ["PG_N8N_PASSWORD"],
        "N8N_ENCRYPTION_KEY": os.environ["N8N_ENCRYPTION_KEY"],
        "N8N_LISTEN_ADDRESS": "127.0.0.1",
        "N8N_PORT": str(PORT),
        "N8N_HOST": "localhost",
        "N8N_PROTOCOL": "http",
        "N8N_USER_FOLDER": str(RUNTIME / "n8n_home"),
        "N8N_DIAGNOSTICS_ENABLED": "false",
        "N8N_PUSH_BACKEND": "sse",              # the Colab proxy is not known to pass WebSockets
        "N8N_PUBLIC_API_DISABLED": "true",
        "N8N_BLOCK_ENV_ACCESS_IN_NODE": "true",
        "GENERIC_TIMEZONE": "Asia/Karachi",
        "TZ": "Asia/Karachi",
    }


def service() -> Service:
    return Service("n8n", [str(RUNTIME / "n8n_pkg" / "bin" / "n8n"), "start"], env=_env(),
                   health=lambda: http_ok(f"http://127.0.0.1:{PORT}/healthz"))
