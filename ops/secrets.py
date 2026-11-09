"""Secret handling for Colab.

Resolution order for each name:
  1. Colab Secrets panel (google.colab.userdata), preferred: never written to disk by us.
  2. $DRIVE_ROOT/secrets/generated.env, values this module generated earlier.
  3. Generate a strong random value (only for GENERATED names) and append it to (2).

Values are exported to os.environ for child processes and never printed; only
masked previews are shown. Drive storage is as private as your Google account;
for production use a real secret manager.
"""
from __future__ import annotations

import os
import secrets as pysecrets
from pathlib import Path

from .env import DRIVE_ROOT

GENERATED = {
    "VOICE_ENGINE_WS_TOKEN": 32,
    "VOICE_ENGINE_GATEWAY_TOKEN": 32,   # Redmi/HFP gateway -> voice engine (control + call media)
    "VOICE_ENGINE_CALLS_TOKEN": 32,     # n8n -> POST /calls
    "LLM_API_KEY": 32,
    "PG_SUPERUSER_PASSWORD": 24,
    "PG_N8N_PASSWORD": 24,
    "PG_OWNER_PASSWORD": 24,
    "PG_APP_PASSWORD": 24,
    "N8N_ENCRYPTION_KEY": 32,      # losing this makes stored n8n credentials unreadable
    "N8N_WEBHOOK_HMAC_SECRET": 32,
}
OPTIONAL = ["HF_TOKEN", "AZURE_SPEECH_KEY", "CLOUDFLARE_TUNNEL_TOKEN", "GIT_REPO_TOKEN"]


def _store() -> Path:
    return DRIVE_ROOT / "secrets" / "generated.env"


def _read_store() -> dict[str, str]:
    p = _store()
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _colab(name: str) -> str | None:
    try:
        from google.colab import userdata  # type: ignore
        return userdata.get(name) or None
    except Exception:          # not in Colab, secret missing, or notebook access not granted
        return None


def mask(v: str) -> str:
    return "(empty)" if not v else (v[:2] + "…" + f"[{len(v)} chars]")


def load_all() -> dict[str, str]:
    """Populate os.environ; return {name: source} (no values)."""
    stored = _read_store()
    sources: dict[str, str] = {}
    new: dict[str, str] = {}
    for name, nbytes in GENERATED.items():
        v = _colab(name)
        src = "colab_secrets"
        if not v:
            v, src = stored.get(name), "drive_generated"
        if not v:
            v, src = pysecrets.token_urlsafe(nbytes), "generated_now"
            new[name] = v
        os.environ[name] = v
        sources[name] = src
    for name in OPTIONAL:
        v = _colab(name) or stored.get(name)
        if v:
            os.environ[name] = v
            sources[name] = "colab_secrets" if _colab(name) else "drive"
        else:
            sources[name] = "not set"
    if new:
        p = _store()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for k, v in new.items():
                fh.write(f"{k}={v}\n")
        try:
            os.chmod(p, 0o600)   # best effort; Drive FUSE may ignore it
        except OSError:
            pass
    return sources
