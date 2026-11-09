"""Gateway CLI (run on the Linux box paired with the Redmi 9C).

  python -m gateway check        --config gateway.yaml   # stack + phone readiness, no call
  python -m gateway dial-test    --config gateway.yaml --number 03xxxxxxxxx --seconds 20
  python -m gateway answer-test  --config gateway.yaml --seconds 20
  python -m gateway run          --config gateway.yaml   # the real service (systemd unit)

dial-test / answer-test exercise the real phone + SIM WITHOUT the AI: they record
the phone's downlink to a WAV and play a countable test signal into the uplink,
then print what was measured. The number must be in policy.outbound_allowlist /
inbound_allowlist. Results are written as JSON next to the WAV.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import testsignal
from .audio import PipeAudio
from .bridge import Bridge
from .config import load
from .ofono import OfonoModem
from .policy import Policy


def _audio(cfg):
    return PipeAudio(cfg.phone_bt_address, cfg.rate, cfg.frame_ms, cfg.playout_latency_ms, cfg.audio_wait_s)


def _sh(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"ERROR {e}"


async def check(cfg) -> int:
    ok = True

    def line(good, name, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"{'OK  ' if good else 'FAIL'} {name} {detail}")

    for tool in ("bluetoothctl", "pactl", "parec", "pacat", "wpctl"):
        line(shutil.which(tool), f"tool {tool}")
    line(_sh(["systemctl", "is-active", "ofono"]) == "active", "ofono service active")
    line(_sh(["systemctl", "is-active", "bluetooth"]) == "active", "bluetooth service active")
    info = _sh(["bluetoothctl", "info", cfg.phone_bt_address]) if cfg.phone_bt_address else "no address configured"
    line("Connected: yes" in info, "phone connected over Bluetooth", cfg.phone_bt_address)
    line("Handsfree" in info or "0000111f" in info.lower(), "phone offers HFP Audio Gateway")
    wp = _sh(["wpctl", "status"])
    print("     wpctl bluetooth lines:", [l.strip() for l in wp.splitlines() if "bluez" in l.lower()][:4])
    modem = OfonoModem(cfg.phone_bt_address)
    try:
        await asyncio.wait_for(modem.start(), 15)
        line(modem.ready(), "oFono HFP modem with VoiceCallManager", str(modem.info()))
    except Exception as e:
        line(False, "oFono HFP modem", f"{type(e).__name__}: {e}")
    if cfg.engine_url:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s, s.get(cfg.engine_url.rstrip("/") + "/healthz", timeout=10) as r:
                body = await r.json(content_type=None)
                line(r.status == 200, "voice engine reachable", json.dumps(body.get("telephony")))
        except Exception as e:
            line(False, "voice engine reachable", str(e))
    print("\nRESULT:", "all checks passed" if ok else "some checks failed (see FAIL lines)")
    return 0 if ok else 1


async def _call_test(cfg, direction: str, number: str | None, seconds: int) -> int:
    policy = Policy(cfg.policy, Path(cfg.state_file))
    modem = OfonoModem(cfg.phone_bt_address)
    await modem.start()
    if not modem.ready():
        print("FAIL: no oFono HFP modem: run `python -m gateway check`")
        return 2
    timeline = []
    t0 = time.monotonic()

    def mark(what):
        timeline.append({"t": round(time.monotonic() - t0, 2), "event": what})
        print(f"[{timeline[-1]['t']:6.2f}s] {what}")

    if direction == "outbound":
        n, reason = policy.check_outbound(number or "")
        if n is None:
            print(f"FAIL: policy refused {number}: {reason}")
            return 2
        policy.record_dial()
        path = await modem.dial(n)
        mark(f"dial {n} -> {path}")
    else:
        mark("waiting up to 120 s for an incoming call from an allowlisted number ...")
        path = None
    active = False
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not active:
        try:
            ev = await asyncio.wait_for(modem.events.get(), timeout=1)
        except asyncio.TimeoutError:
            continue
        mark(f"{ev.kind} {ev.state} {ev.number}".strip())
        if direction == "inbound" and ev.kind == "added" and ev.state == "incoming" and path is None:
            ok, why = policy.should_answer(ev.number)
            if not ok:
                mark(f"not answering {ev.number}: {why}")
                continue
            path = ev.path
            await asyncio.sleep(1.0)
            await modem.answer(path)
            mark("answer sent")
        if ev.path == path and ev.state == "active":
            active = True
        if ev.path == path and ev.kind == "removed":
            break
    result = {"direction": direction, "number": number, "timeline": timeline, "call_active": active}
    if active:
        audio = _audio(cfg)
        try:
            await audio.open()
            mark(f"audio nodes {audio.nodes}")
            audio.play(testsignal.bursts_pcm(cfg.rate))
            rec = bytearray()
            end = time.monotonic() + seconds
            async for frame in audio.frames():
                rec.extend(frame)
                if time.monotonic() > end:
                    break
            out = Path(cfg.recordings_dir) / f"{direction}_{time.strftime('%Y%m%d-%H%M%S')}.wav"
            testsignal.write_wav(out, bytes(rec), cfg.rate)
            result.update(audio_nodes=audio.nodes, downlink_wav=str(out), downlink_seconds=len(rec) / 2 / cfg.rate,
                          downlink_rms_dbfs=round(testsignal.rms_dbfs(bytes(rec)), 1),
                          uplink_test_signal_groups=list(testsignal.GROUPS))
            mark(f"recorded {result['downlink_seconds']:.1f}s downlink, RMS {result['downlink_rms_dbfs']} dBFS -> {out}")
        except Exception as e:
            result["audio_error"] = f"{type(e).__name__}: {e}"
            mark(f"AUDIO FAIL: {e}")
        finally:
            await audio.close()
        await modem.hangup(path)
        mark("hangup sent")
    print("\nVerify by ear:")
    print(f"  1. The other phone should have heard bursts in groups of {list(testsignal.GROUPS)}.")
    print("  2. Play the downlink WAV: it should contain the other phone's voice.")
    print("  A recording near -90 dBFS or lower is silence (no downlink audio).")
    out_json = Path(cfg.recordings_dir) / f"{direction}_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("result:", out_json)
    return 0 if active and "audio_error" not in result else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="gateway")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "run", "dial-test", "answer-test"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        p.add_argument("--log-level", default="INFO")
        if name == "dial-test":
            p.add_argument("--number", required=True)
        if name in ("dial-test", "answer-test"):
            p.add_argument("--seconds", type=int, default=20)
    a = ap.parse_args()
    logging.basicConfig(level=a.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load(a.config)
    if a.cmd == "check":
        return asyncio.run(check(cfg))
    if a.cmd == "dial-test":
        return asyncio.run(_call_test(cfg, "outbound", a.number, a.seconds))
    if a.cmd == "answer-test":
        return asyncio.run(_call_test(cfg, "inbound", None, a.seconds))
    if not cfg.engine_url:
        sys.exit("engine_url is not set (config or VOICE_GATEWAY_ENGINE_URL)")
    _ = cfg.token
    bridge = Bridge(cfg, OfonoModem(cfg.phone_bt_address), lambda: _audio(cfg), Policy(cfg.policy, Path(cfg.state_file)))
    asyncio.run(bridge.run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
