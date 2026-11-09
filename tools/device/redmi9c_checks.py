#!/usr/bin/env python3
"""Redmi 9C Phase 1 device checks (T1-T4). Runs on any computer with adb + Python 3.9+.

  python tools/device/redmi9c_checks.py              # all tests, guided
  python tools/device/redmi9c_checks.py --only t1,t2 # automatic tests only

T1 and T2 are fully automatic. T3 and T4 need a live call from a second phone;
the script captures Android's own audio-policy state during the call and asks
you what you heard. Your answers are stored as "user_reported": the script never
infers a pass for a test that depends on human observation.

Results: docs/device-tests/results/redmi9c_<timestamp>/ (summary.json + raw dumps).
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

EXPECTED_MODEL = "M2006C3MG"
OUT_ROOT = Path(__file__).resolve().parents[2] / "docs" / "device-tests" / "results"


class Adb:
    def __init__(self, serial: str | None):
        self.base = ["adb"] + (["-s", serial] if serial else [])

    def sh(self, cmd: str, timeout: int = 30) -> str:
        r = subprocess.run(self.base + ["shell", cmd], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return (r.stdout + (r.stderr if r.returncode else "")).strip()

    def push(self, local: Path, remote: str) -> str:
        r = subprocess.run(self.base + ["push", str(local), remote], capture_output=True, text=True, timeout=60)
        return (r.stdout + r.stderr).strip()


def ask(prompt: str, choices: list[str]) -> str:
    while True:
        a = input(f"{prompt} [{'/'.join(choices)}]: ").strip().lower()
        if a in choices:
            return a
        print("  please answer one of:", ", ".join(choices))


def wait_enter(msg: str) -> None:
    input(f"\n>>> {msg}\n    Press Enter when done...")


def preflight(serial: str | None) -> Adb:
    if not shutil.which("adb"):
        sys.exit("adb not found on PATH (install Android platform-tools)")
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    devs = [l.split() for l in out.splitlines()[1:] if l.strip()]
    if not devs:
        sys.exit("No device. Enable USB debugging, connect USB, accept the RSA prompt on the phone.")
    unauth = [d for d in devs if d[1] != "device"]
    if unauth:
        sys.exit(f"Device not ready: {unauth}. Accept the debugging prompt on the phone.")
    if serial is None and len(devs) > 1:
        sys.exit(f"Several devices {[d[0] for d in devs]}: pass --serial")
    return Adb(serial or devs[0][0])


def t1_abi(adb: Adb) -> dict:
    props = {k: adb.sh(f"getprop {k}") for k in (
        "ro.product.model", "ro.product.cpu.abilist", "ro.product.cpu.abilist64", "ro.product.cpu.abi",
        "ro.build.version.release", "ro.build.version.sdk", "ro.miui.ui.version.name",
        "ro.build.version.incremental", "ro.build.fingerprint")}
    abis = [a for a in props["ro.product.cpu.abilist"].split(",") if a]
    has64 = "arm64-v8a" in abis
    res = {"props": props, "has_arm64_v8a": has64,
           "model_matches": props["ro.product.model"] == EXPECTED_MODEL,
           "verdict": ("arm64-v8a present: the shipped PJSIP AAR could load" if has64 else
                       "NO arm64-v8a: Rappelo APK as shipped cannot load its PJSIP native library")}
    print(f"T1 model={props['ro.product.model']} android={props['ro.build.version.release']} "
          f"miui={props['ro.miui.ui.version.name']} abilist={abis}\n   -> {res['verdict']}")
    return res


def t2_permissions(adb: Adb) -> dict:
    out = adb.sh("pm list permissions -f", timeout=60)
    want = ["android.permission.CAPTURE_AUDIO_OUTPUT", "android.permission.MODIFY_PHONE_STATE",
            "android.permission.CAPTURE_AUDIO_HOTWORD", "android.permission.RECORD_AUDIO",
            "android.permission.ANSWER_PHONE_CALLS"]
    levels = {}
    blocks = out.split("+ permission:")
    for b in blocks[1:]:
        name = b.splitlines()[0].strip()
        if name in want:
            m = re.search(r"protectionLevel:(\S+)", b)
            levels[name] = m.group(1) if m else "unknown"
    dialer = adb.sh("cmd role get-role-holders android.app.role.DIALER")
    res = {"protection_levels": levels, "dialer_role_holder": dialer,
           "privileged_only": {k: ("privileged" in v or "signature" in v) for k, v in levels.items()}}
    for k, v in levels.items():
        print(f"T2 {k.rsplit('.', 1)[1]:26} {v}")
    print(f"T2 current default dialer: {dialer or '(unknown)'}")
    return res


def _call_state(adb: Adb) -> list[int]:
    out = adb.sh("dumpsys telephony.registry")
    return [int(x) for x in re.findall(r"mCallState=(\d)", out)]


def _recording_clients(adb: Adb, dump_dir: Path, tag: str) -> dict:
    audio = adb.sh("dumpsys audio", timeout=60)
    policy = adb.sh("dumpsys media.audio_policy", timeout=60)
    (dump_dir / f"{tag}_dumpsys_audio.txt").write_text(audio, encoding="utf-8")
    (dump_dir / f"{tag}_dumpsys_audio_policy.txt").write_text(policy, encoding="utf-8")
    mode = re.search(r"mMode\s*=\s*(\S+)|Mode:\s*(\S+)", audio)
    uids = sorted(set(re.findall(r"uid[:= ](\d{4,6})", policy + "\n" + audio)))
    pkgs = adb.sh("pm list packages -U")
    system_pkgs = set(re.findall(r"package:(\S+)", adb.sh("pm list packages -s")))
    uid_pkgs = {}
    for line in pkgs.splitlines():
        m = re.match(r"package:(\S+) uid:(\d+)", line.strip())
        if m and m.group(2) in uids:
            uid_pkgs.setdefault(m.group(2), []).append(m.group(1))
    silenced_lines = [l.strip() for l in (policy + "\n" + audio).splitlines() if re.search(r"silenc", l, re.I)][:20]
    return {"audio_mode_line": (mode.group(0) if mode else None), "uid_packages": uid_pkgs,
            "third_party_recorders": sorted({p for ps in uid_pkgs.values() for p in ps if p not in system_pkgs}),
            "silenced_lines": silenced_lines}


def _wait_offhook(adb: Adb, timeout_s: int = 180) -> bool:
    print("   waiting for an active call (OFFHOOK) ...", end="", flush=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if 2 in _call_state(adb):
            print(" call active")
            return True
        print(".", end="", flush=True)
        time.sleep(2)
    print(" timeout")
    return False


def t3_capture(adb: Adb, dump_dir: Path) -> dict:
    print("\nT3: can an ordinary app record the microphone during a cellular call?")
    print("   Install a THIRD-PARTY voice recorder from the Play Store first (not MIUI's Recorder).")
    wait_enter("From a SECOND phone, call the Redmi 9C and answer it. Turn on SPEAKERPHONE.")
    active = _wait_offhook(adb)
    wait_enter("Open the third-party recorder and START recording. Talk from both phones for ~20 s, "
               "keep the recording running while you press Enter.")
    during = _recording_clients(adb, dump_dir, "t3_during_call")
    print(f"   recording clients seen: {during['uid_packages']}")
    print(f"   third-party recorders: {during['third_party_recorders'] or 'none detected'}")
    if during["silenced_lines"]:
        print("   audio policy lines mentioning 'silenced':", *during["silenced_lines"][:5], sep="\n     ")
    wait_enter("STOP the recording, hang up, and play the recording back.")
    heard = ask("   On playback, was the CALLER's voice audible?", ["silent", "faint", "clear"])
    own = ask("   Was YOUR own voice (Redmi side) audible?", ["silent", "faint", "clear"])
    return {"call_active_detected": active, "during_call": during,
            "user_reported": {"caller_voice": heard, "own_voice": own},
            "interpretation": ("matches Android 10 policy: ordinary app silenced during call" if heard == "silent"
                               else "MIUI deviates from AOSP policy or recorder uses accessibility; review dumps")}


def _speech_like_wav(path: Path, groups=(2, 3, 5)) -> None:
    """Vowel-like harmonic bursts (not pure tones, which noise suppression removes).
    The listener counts bursts per group; expected counts are recorded in the results."""
    sr, out = 16000, []
    f0 = 140.0
    for gi, n in enumerate(groups):
        for _ in range(n):
            dur = int(0.35 * sr)
            for i in range(dur):
                t = i / sr
                env = math.sin(math.pi * i / dur) ** 2 * (0.5 + 0.5 * math.sin(2 * math.pi * 5 * t))
                s = sum((1 / k) * math.sin(2 * math.pi * f0 * k * t) for k in range(1, 18) if f0 * k < 3400)
                out.append(0.35 * env * s)
            out += [0.0] * int(0.25 * sr)
        out += [0.0] * int(1.5 * sr)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x)) * 32767)) for x in out))


def t4_injection(adb: Adb, dump_dir: Path) -> dict:
    print("\nT4: does audio played on the Redmi's speaker reach the caller during a call?")
    wav = dump_dir / "va_t4_bursts.wav"
    groups = (2, 3, 5)
    _speech_like_wav(wav, groups)
    remote = "/sdcard/Download/va_t4_bursts.wav"
    print("  ", adb.push(wav, remote))
    wait_enter("From the SECOND phone, call the Redmi 9C, answer, turn on SPEAKERPHONE. "
               "The second-phone person stays silent and listens.")
    _wait_offhook(adb)
    print("   launching playback on the Redmi (if no player opens, open Files > Download > va_t4_bursts.wav)")
    adb.sh(f"am start -a android.intent.action.VIEW -d file://{remote} -t audio/wav")
    wait_enter("Let it play to the end (~12 s) at FULL media volume.")
    during = _recording_clients(adb, dump_dir, "t4_during_call")
    print(f"   expected bursts per group: {list(groups)}")
    counts = input("   How many bursts did the far-end listener count in each group? (e.g. 2,3,5 or 0,0,0): ").strip()
    quality = ask("   How did it sound at the far end?", ["none", "faded_out", "faint", "garbled", "clear"])
    wait_enter("Optional but recommended: during another call, play a spoken podcast/video at full volume "
               "and note whether the words are understandable at the far end.")
    speech = ask("   Spoken-word clip at far end", ["not_tested", "not_heard", "unintelligible", "intelligible"])
    return {"expected_groups": list(groups), "user_reported": {"burst_counts": counts, "quality": quality,
                                                               "spoken_clip": speech},
            "audio_state_during": during}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial")
    ap.add_argument("--only", default="t1,t2,t3,t4")
    a = ap.parse_args()
    tests = set(a.only.lower().split(","))
    adb = preflight(a.serial)
    run_dir = OUT_ROOT / f"redmi9c_{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "tests": {}}
    for name, fn in (("t1", lambda: t1_abi(adb)), ("t2", lambda: t2_permissions(adb)),
                     ("t3", lambda: t3_capture(adb, run_dir)), ("t4", lambda: t4_injection(adb, run_dir))):
        if name in tests:
            try:
                summary["tests"][name] = fn()
            except KeyboardInterrupt:
                summary["tests"][name] = {"status": "aborted by user"}
                break
            except Exception as e:
                summary["tests"][name] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
                print(f"{name} error: {e}")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResults saved to {run_dir}\nShare summary.json (raw dumps contain your phone's app list; "
          "review before sharing).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
