"""One-call health/diagnostic report for every component."""
from __future__ import annotations

import json
import os
import subprocess

from . import llm_server, n8n, pg
from .env import disk_report
from .services import http_ok


def _gpu() -> str:
    r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                        "--format=csv,noheader"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "NO GPU"


def _pg_login(role: str, env: str, db: str) -> bool:
    r = subprocess.run([str(pg.BIN / "psql"), "-h", "127.0.0.1", "-p", str(pg.PORT), "-U", role, "-d", db,
                        "-tAc", "select 1"], capture_output=True, text=True,
                       env=dict(os.environ, PGPASSWORD=os.environ.get(env, ""), PGCONNECT_TIMEOUT="3"))
    return r.returncode == 0 and r.stdout.strip() == "1"


def report(public_url: str | None = None) -> dict:
    key = os.environ.get("LLM_API_KEY", "")
    rep = {
        "gpu": _gpu(),
        "disk": disk_report(),
        "postgres_up": pg.service().running(),
        "pg_login_va_owner": _pg_login("va_owner", "PG_OWNER_PASSWORD", "voiceagent"),
        "pg_login_va_app": _pg_login("va_app", "PG_APP_PASSWORD", "voiceagent"),
        "pg_login_n8n_sys": _pg_login("n8n_sys", "PG_N8N_PASSWORD", "n8n"),
        "pg_va_app_denied_n8n_db": not _pg_login("va_app", "PG_APP_PASSWORD", "n8n"),   # isolation check
        "latest_backup_voiceagent": str(pg.latest_backup("voiceagent") or "none"),
        "llm_server": http_ok(f"http://127.0.0.1:{llm_server.PORT}/health",
                              {"Authorization": f"Bearer {key}"} if key else {}),
        "voice_engine_local": http_ok("http://127.0.0.1:8765/healthz"),
        "n8n": http_ok(f"http://127.0.0.1:{n8n.PORT}/healthz"),
    }
    if public_url:
        rep["voice_engine_public"] = http_ok(f"{public_url}/healthz", timeout=10)
    return rep


def print_report(public_url: str | None = None) -> dict:
    rep = report(public_url)
    for k, v in rep.items():
        mark = "OK  " if v is True else ("FAIL" if v is False else "    ")
        print(f"{mark} {k}: {json.dumps(v) if not isinstance(v, bool) else ''}")
    return rep
