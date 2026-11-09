#!/usr/bin/env python3
"""
Static audit of the Rappelo GSM<->SIP gateway repository.

Runs identically in Google Colab and locally (stdlib only):

    python tools/audit_rappelo.py --repo /content/drive/MyDrive/voiceagent/vendor/rappelo
    python tools/audit_rappelo.py --repo ./vendor/rappelo --clone   # clone first if missing

What it checks (static facts only -- it does NOT prove runtime behaviour):
  * Native ABIs shipped in the PJSIP AAR (Redmi 9C ships a 32-bit userspace;
    see docs/phase1-rappelo-audit.md).
  * Manifest permissions whose Android protection level makes them
    unobtainable for an unrooted, user-installed app.
  * Which AudioRecord sources the bridge tries, and whether it contains the
    only non-privileged in-call capture path (an AccessibilityService).
  * Whether the audio bridge is acoustic (speakerphone -> mic).
  * Server-side requirements (Docker, privileged containers, public UDP).

Exit code: 0 = audit ran (findings may still be blockers), 2 = repo missing/unreadable.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_URL = "https://github.com/911nimo/rappelo-bridge-voip-gateway-voice-sms"

# Protection levels from AOSP frameworks/base/core/res/AndroidManifest.xml (Android 10).
# "signature|privileged" => only platform-signed or /system/priv-app apps can hold it.
# `adb shell pm grant` only works for runtime ("dangerous") or "development" permissions.
PRIVILEGED_PERMISSIONS = {
    "android.permission.CAPTURE_AUDIO_OUTPUT": "signature|privileged",
    "android.permission.MODIFY_PHONE_STATE": "signature|privileged",
}
# PROCESS_OUTGOING_CALLS is a normal runtime permission but deprecated in API 29.
DEPRECATED_PERMISSIONS = {
    "android.permission.PROCESS_OUTGOING_CALLS": "deprecated in API 29 (use CallRedirectionService)",
}

AUDIO_SOURCES_NEED_PRIVILEGE = {"VOICE_DOWNLINK", "VOICE_UPLINK", "VOICE_CALL"}


def clone(repo: Path) -> None:
    repo.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "50", REPO_URL, str(repo)], check=True)


def read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def audit(repo: Path) -> dict:
    mobile = repo / "mobile"
    kotlin_files = list((mobile / "app/src/main/kotlin").rglob("*.kt"))
    kotlin_src = {str(f.relative_to(repo)): read(f) for f in kotlin_files}
    all_kotlin = "\n".join(kotlin_src.values())
    manifest = read(mobile / "app/src/main/AndroidManifest.xml")

    findings: list[dict] = []

    def add(fid: str, severity: str, title: str, evidence: str, consequence: str) -> None:
        findings.append(dict(id=fid, severity=severity, title=title,
                             evidence=evidence, consequence=consequence))

    # --- 1. Native ABIs -------------------------------------------------------
    aar = mobile / "app/libs/pjsua2.aar"
    abis: list[str] = []
    if aar.exists():
        with zipfile.ZipFile(aar) as z:
            abis = sorted({n.split("/")[1] for n in z.namelist()
                           if n.startswith("jni/") and n.count("/") >= 2 and n.endswith(".so")})
        if abis == ["arm64-v8a"]:
            add("ABI-64ONLY", "BLOCKER-IF-32BIT",
                "PJSIP AAR ships only arm64-v8a native libraries",
                f"{aar.relative_to(repo)} jni ABIs = {abis}",
                "On a device whose ro.product.cpu.abilist has no arm64-v8a (Redmi 9C stock MIUI is "
                "reported as 32-bit userspace) the APK fails to install or crashes with "
                "UnsatisfiedLinkError. PJSIP must be rebuilt for armeabi-v7a.")
    else:
        add("ABI-MISSING", "HIGH", "pjsua2.aar not present", str(aar), "SIP stack cannot load.")

    # --- 2. Privileged permissions ------------------------------------------
    declared = set(re.findall(r'android:name="(android\.permission\.[A-Z_]+)"', manifest))
    for perm, level in PRIVILEGED_PERMISSIONS.items():
        if perm in declared:
            add(f"PERM-{perm.rsplit('.', 1)[1]}", "HIGH",
                f"{perm} is {level}",
                "Declared in AndroidManifest.xml",
                "Cannot be granted to a user-installed app on an unrooted phone; "
                "'adb shell pm grant' rejects it. Code paths depending on it are dead.")
    for perm, why in DEPRECATED_PERMISSIONS.items():
        if perm in declared:
            add(f"PERM-{perm.rsplit('.', 1)[1]}", "LOW", f"{perm} {why}", "Declared", "Minor.")

    readme = read(repo / "README.md")
    if "rooted device" in readme:
        add("README-ROOT", "INFO", "README documents system-app install on a rooted device",
            "README.md: 'Deploy as System App (rooted device)'",
            "The authors' own intended deployment for full functionality is root/system-app.")

    # --- 3. Audio capture path ----------------------------------------------
    sources = re.findall(r"MediaRecorder\.AudioSource\.([A-Z_]+)", all_kotlin)
    privileged_sources = sorted(set(sources) & AUDIO_SOURCES_NEED_PRIVILEGE)
    fallback_sources = sorted(set(sources) - AUDIO_SOURCES_NEED_PRIVILEGE)
    has_a11y = bool(re.search(r"AccessibilityService", all_kotlin)) or \
        "android.accessibilityservice" in manifest
    if privileged_sources:
        add("AUDIO-SRC", "BLOCKER",
            "Call-audio capture falls back to the microphone on unrooted devices",
            f"Sources tried: {sources}; privileged-only: {privileged_sources}; "
            f"fallbacks: {fallback_sources}",
            "Without CAPTURE_AUDIO_OUTPUT the bridge records the physical MIC. Per Android 10 "
            "audio-input sharing policy, during a cellular call an ordinary app's capture is "
            "silenced unless it is an accessibility service.")
    if not has_a11y:
        add("AUDIO-NO-A11Y", "BLOCKER",
            "No AccessibilityService in the app",
            "No AccessibilityService class or manifest declaration found",
            "On Android 10 the only documented non-privileged way to capture mic audio during a "
            "voice call is an accessibility service. As written, GSM->SIP audio is expected to be "
            "silence on an unrooted Redmi 9C (must be confirmed on device: test T3).")

    acoustic = "speaker" in all_kotlin.lower() and "Acoustic audio bridge" in all_kotlin
    if acoustic:
        add("AUDIO-ACOUSTIC", "BLOCKER-FOR-COMMERCIAL",
            "Audio bridge is acoustic (speakerphone -> microphone), not line-level",
            "SipAudioBridge.kt: GSM downlink -> speaker -> mic; SIP audio -> AudioTrack(USAGE_MEDIA) "
            "-> speaker -> mic -> GSM uplink; AEC and NS explicitly disabled",
            "Both directions pass through a cheap speaker and mic in open air: echo, room noise, "
            "feedback, the AI hears its own TTS, and the modem's in-call echo canceller may remove "
            "the injected TTS from the uplink. Not acceptable for commercial call quality.")

    if "8000" in read(mobile / "app/src/main/kotlin/com/gateway/telephony/sip/SipAudioBridge.kt"):
        add("AUDIO-8K", "INFO", "Bridge runs at 8 kHz L16 (narrowband)",
            "SipAudioBridge.sampleRate = 8000", "STT accuracy (esp. Urdu) is limited by narrowband audio.")

    # --- 4. Call control ------------------------------------------------------
    uses_incall = "InCallService" in all_kotlin and "placeCall" in all_kotlin
    requests_role = "createRequestRoleIntent" in all_kotlin or "ACTION_CHANGE_DEFAULT_DIALER" in all_kotlin
    if uses_incall:
        add("CALL-CTRL", "OK-WITH-CAVEAT",
            "Call control uses default-dialer InCallService + TelecomManager.placeCall",
            "GatewayInCallService.kt, GsmCallManagerImpl.kt",
            "Answer / place / hang up work without root if the app is the default phone app. "
            + ("" if requests_role else "The app never requests ROLE_DIALER; the user must set it "
               "manually, and MIUI may override it."))

    # --- 5. Server side -------------------------------------------------------
    compose = read(repo / "docker-compose.yml")
    if compose:
        priv = "privileged: true" in compose
        udp = re.findall(r'"([\d\-${}:A-Z_]+/udp)"', compose)
        add("SRV-DOCKER", "BLOCKER-IN-COLAB",
            "SIP server stack is Docker Compose with public UDP ports"
            + (" and a privileged container" if priv else ""),
            f"UDP mappings: {udp}",
            "Colab has no Docker daemon and no inbound UDP. Cloudflare Tunnel carries HTTP/TCP, "
            "not SIP-over-UDP or RTP. Kamailio/RTPEngine/coturn need a VPS with a public IP.")

    summary = {
        "repo": str(repo),
        "head": subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%h %s"],
                               capture_output=True, text=True).stdout.strip(),
        "kotlin_files": len(kotlin_files),
        "pjsip_abis": abis,
        "audio_sources_tried": sources,
        "has_accessibility_service": has_a11y,
        "acoustic_bridge": acoustic,
        "findings": findings,
    }
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--clone", action="store_true", help="clone the repo if it is missing")
    ap.add_argument("--json", type=Path, help="also write findings as JSON to this path")
    args = ap.parse_args()

    if not (args.repo / "mobile").is_dir():
        if args.clone:
            clone(args.repo)
        else:
            print(f"ERROR: {args.repo} is not a Rappelo checkout (use --clone)", file=sys.stderr)
            return 2

    result = audit(args.repo)
    print(f"Rappelo audit @ {result['head']}  ({result['kotlin_files']} Kotlin files)")
    print(f"PJSIP ABIs: {result['pjsip_abis']}")
    print(f"AudioRecord sources: {result['audio_sources_tried']}")
    print(f"AccessibilityService present: {result['has_accessibility_service']}")
    print(f"Acoustic bridge: {result['acoustic_bridge']}\n")
    for f in result["findings"]:
        print(f"[{f['severity']}] {f['id']}: {f['title']}")
        print(f"    evidence:    {f['evidence']}")
        print(f"    consequence: {f['consequence']}\n")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"JSON written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
