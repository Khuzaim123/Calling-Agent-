"""Cloudflare Tunnel for the voice engine's HTTPS/WebSocket endpoint only.

* Quick tunnel (default): random https://<words>.trycloudflare.com URL, no account,
  changes every start, intended for testing.
* Named tunnel: set CLOUDFLARE_TUNNEL_TOKEN (Colab secret) for a stable hostname
  configured in the Cloudflare dashboard to point at http://127.0.0.1:8765.

Cloudflare Tunnel carries HTTP(S)/WebSocket here, not SIP or RTP. That is enough: the
Redmi 9C HFP gateway connects outbound to /ws/gateway and /ws (docs/telephony-redmi9c-gateway.md).
Only the voice engine is published. PostgreSQL, n8n and llama-server stay on 127.0.0.1.
"""
from __future__ import annotations

import os
import re
import stat
import time
import urllib.request
from pathlib import Path

from .env import DRIVE_ROOT, RUNTIME
from .services import Service

CLOUDFLARED_VERSION = os.environ.get("CLOUDFLARED_VERSION", "2026.10.1")   # 2026-10-15
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def ensure_cloudflared() -> Path:
    exe = DRIVE_ROOT / "cache" / "cloudflared" / f"cloudflared-{CLOUDFLARED_VERSION}"
    local = RUNTIME / "run" / "cloudflared"
    if not exe.exists():
        url = (f"https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}/"
               "cloudflared-linux-amd64")
        print("downloading", url)
        tmp = exe.with_suffix(".partial")
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(exe)
    local.write_bytes(exe.read_bytes())          # Drive FUSE may not allow exec
    local.chmod(local.stat().st_mode | stat.S_IEXEC)
    return local


def service(target: str = "http://127.0.0.1:8765") -> Service:
    exe = str(RUNTIME / "run" / "cloudflared")
    token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "")
    # --protocol http2: cloudflared defaults to QUIC (UDP 7844), which Colab's network may block;
    # http2 runs over TCP 443. Observed 2026-11-08: the quick-tunnel URL never resolved with the default.
    if token:
        # token via TUNNEL_TOKEN env so it is not visible in the process list
        return Service("tunnel", [exe, "tunnel", "--no-autoupdate", "--protocol", "http2", "run"],
                       env={"TUNNEL_TOKEN": token})
    return Service("tunnel", [exe, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", target])


def connected(log_text: str) -> bool:
    return "Registered tunnel connection" in log_text


def current_run(log_text: str) -> str:
    """Only the part of the (appended, persistent) log written by the latest start.

    Bug fixed 2026-11-08: reading the whole log returned the previous run's URL."""
    marker = log_text.rfind("===== start ")
    return log_text[marker:] if marker >= 0 else log_text


def public_url(svc: Service, timeout_s: float = 90) -> str | None:
    """The tunnel URL once cloudflared has actually registered a connection with Cloudflare.

    The trycloudflare URL appears in the log before the connection exists; a URL printed at that
    point does not resolve (DNS NXDOMAIN) until registration succeeds.
    """
    deadline = time.time() + timeout_s
    url = os.environ.get("VOICE_ENGINE_PUBLIC_URL") if os.environ.get("CLOUDFLARE_TUNNEL_TOKEN") else None
    while time.time() < deadline:
        log_text = current_run(svc.tail(300))
        found = URL_RE.findall(log_text)
        if found:
            url = found[-1]
        if url and connected(log_text):
            return url
        if not svc.running():
            break
        time.sleep(2)
    problems = [ln for ln in current_run(svc.tail(300)).splitlines()
                if " ERR " in ln or "error" in ln.lower()][-10:]
    print("TUNNEL NOT CONNECTED. cloudflared log (errors):\n  " + "\n  ".join(problems or ["(none logged)"]))
    return None
