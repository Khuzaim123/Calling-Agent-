# Phase 1: Rappelo Repository Audit

> **Follow-up (2026-11-07):** the Redmi 9C is now the telephony endpoint through **Bluetooth HFP** to a small
> Linux gateway (`docs/telephony-redmi9c-gateway.md`). That path gives line-level two-way call audio without
> root or any phone app, which sidesteps the app-level audio restrictions documented below. The findings
> below are unchanged and still explain why Rappelo's app-based approach cannot work unrooted.

| | |
|---|---|
| Repository | https://github.com/911nimo/rappelo-bridge-voip-gateway-voice-sms |
| Commit audited | `ba389d3` "Add InCallService and acoustic audio bridge" (HEAD, 2026-11-07) |
| Target device | Redmi 9C (M2006C3MG), Android 10, MIUI 12.0.23, **unrooted** |
| Method | Full source read of the call, audio, and SIP paths + `tools/audit_rappelo.py` (executed; output in `docs/audit/rappelo-audit-results.json`) |
| Not done | APK build, on-device execution. **No runtime claim in this report has been tested on the phone.** |

## Verdict

**Rappelo cannot provide commercial-quality two-way cellular call audio on an unrooted Redmi 9C.**
This is not a bug in Rappelo that we can fix. It is how Android works:

> **Android 10, "Voice call + ordinary app":** "The call always receives audio. The app can capture audio if it is an accessibility service. The app can capture the voice call if it is a privileged (pre-installed) app with permission `CAPTURE_AUDIO_OUTPUT`."
> — [developer.android.com/media/platform/sharing-audio-input](https://developer.android.com/media/platform/sharing-audio-input)

An unrooted phone cannot give a user-installed app `CAPTURE_AUDIO_OUTPUT`. No public Android API lets an ordinary app *inject* audio into the cellular uplink. The only remaining path is **acoustic**: put the call on speakerphone, record it through the microphone, and play the AI voice out of the speaker so the microphone picks it up. That is exactly what Rappelo's latest commit does.

## What the code actually does

### Audio (the deciding component): `SipAudioBridge.kt`
```
GSM downlink → loudspeaker → air → microphone → AudioRecord → PJSIP → SIP/RTP
SIP/RTP → PJSIP → AudioTrack(USAGE_MEDIA) → loudspeaker → air → microphone → GSM uplink
```
- It tries the `VOICE_DOWNLINK` and then `VOICE_CALL` capture sources first. Both need `CAPTURE_AUDIO_OUTPUT`, so on your phone it falls back to `UNPROCESSED` or `MIC`.
- It explicitly **disables** echo cancellation and noise suppression. It has to, or the platform would cancel the speaker audio it is trying to record.
- Audio is 8 kHz narrowband.
- **There is no AccessibilityService.** Under the policy quoted above, its microphone capture during a cellular call is expected to receive **silence** on Android 10. Test T3 confirms or refutes this on your phone.

Even if capture works, an acoustic bridge has these problems, and none can be fixed in software:
1. The microphone hears the caller **and** the AI's own TTS from the same speaker. The STT therefore transcribes the AI.
2. The modem's in-call echo canceller treats speaker output as echo. It may strip the injected AI voice from the uplink, so the customer hears little or nothing. **Unverified on this device; test T4.**
3. Room noise, speaker distortion at full volume, and feedback.
4. The phone must sit in a quiet enclosure with the speaker at maximum volume.

### Call control: `GatewayInCallService.kt`, `GsmCallManagerImpl.kt`. This part is viable.
- Calls are answered, placed, and hung up through the default-dialer `InCallService` and `TelecomManager.placeCall`. These are legitimate non-root APIs on Android 10, *provided the app is set as the default phone app*.
- Caveat: the app never asks for `ROLE_DIALER`, so you must set it manually, and MIUI may interfere. Not tested.

### Permissions (`AndroidManifest.xml`)
| Permission | Status on unrooted phone |
|---|---|
| `CAPTURE_AUDIO_OUTPUT` | `signature\|privileged`, **unobtainable**. The code comment `adb shell pm grant …` will be rejected. |
| `MODIFY_PHONE_STATE` | `signature\|privileged`, **unobtainable** |
| `PROCESS_OUTGOING_CALLS` | deprecated in API 29 |
| `CALL_PHONE`, `ANSWER_PHONE_CALLS`, `RECORD_AUDIO`, `READ_PHONE_STATE` | obtainable as runtime permissions |

The README itself documents "Deploy as System App (**rooted device**)".

### Native library: likely a hard install blocker
`mobile/app/libs/pjsua2.aar` contains **only `arm64-v8a`** native libraries. The Redmi 9C's Helio G35 is a 64-bit CPU, but stock MIUI is widely reported to ship a **32-bit userspace**. If `ro.product.cpu.abilist` has no `arm64-v8a`, the APK will not install or will crash on launch. The fix would be rebuilding PJSIP for `armeabi-v7a`, which is feasible but irrelevant given the audio verdict. **Test T1 settles this in 10 seconds.**

### Server stack: `docker-compose.yml`
Kamailio, RTPEngine (as a **privileged** container), coturn, PostgreSQL, and a FastAPI admin panel. It needs public **UDP** 5060, 3478, 30000–30100, and 49152–49200.
- **It cannot run in Colab.** Colab has no Docker daemon and no inbound UDP.
- **Cloudflare Tunnel does not help.** It carries HTTP and TCP to the origin, not SIP-over-UDP or RTP.
- It also contains a second, separate business backend (the FastAPI admin panel), which your architecture rules exclude.

### Summary table
| Capability | Status |
|---|---|
| Builds | **Not tested** (Gradle/AGP 8.11 and JDK 17 needed; arm64-only AAR) |
| Installs on Redmi 9C | **Blocked if 32-bit userspace** (T1) |
| SIP registration | Not tested. Would not prove audio works, as you noted. |
| Answer incoming SIM call | Implemented. Not tested (needs default-dialer role). |
| Place outgoing SIM call | Implemented. Not tested. |
| Capture caller audio | **Blocked by Android 10 policy** without an accessibility service (T3 to confirm) |
| Inject AI audio into call | **Acoustic only.** Quality and survival through modem AEC untested (T4). |
| Full duplex | Acoustic only: the AI hears itself |
| Screen-off / MIUI background | Not tested. MIUI battery saver and autostart restrictions are known risks. |
| Server in Colab | **Blocked** (Docker + UDP) |

## Regulatory note (Pakistan)
Routing calls from SIM cards into VoIP through unlicensed gateways is the pattern PTA prosecutes as illegal "grey" telephony. A business answering its *own* calls on its *own* SIM with an automated agent is different in intent, but a commercial service that bridges many customers' SIMs into a VoIP platform needs a **written legal opinion and the right PTA licensing/type-approval** before launch. I have not treated any gateway option as legally cleared.

## Telephony options that remain

| Option | Audio | Fits constraints? | Notes |
|---|---|---|---|
| **A. Licensed SIP trunk + carrier call forwarding** | Line-level, clean | Needs paid telephony + a small public VPS for SIP/RTP | BYON works: the business keeps its number and sets carrier call forwarding (e.g. on no-answer or all calls) to a DID on our trunk. Outbound caller ID is set by the trunk provider's policy. This is the production-grade path and it scales past one call. |
| **B. Hardware GSM-to-SIP gateway** (commercial GSM gateway, or an LTE modem with USB voice + Asterisk on a Raspberry Pi) | Line-level | Uses ordinary SIMs; needs an always-on box (not Colab, not your laptop) | Gives real two-way audio from a SIM. Same PTA question as above, plus device type-approval. One call per SIM/module. |
| **C. Unrooted Redmi 9C, acoustic bridge + accessibility capture** | Acoustic, poor | Matches your hardware | Good for a lab experiment, not for a product. I recommend not building the platform on it. |
| **D. Root / custom ROM** | Line-level possible | **Violates your "unrooted" constraint**, and MediaTek audio HAL support is uncertain | Excluded. |

Under your rule 17 ("no paid telephony unless there is no technically viable alternative"), this audit is the demonstration that **no unrooted Android path gives line-level call audio**. So option A or B is justified.

## Impact on the rest of the plan
- **The AI voice pipeline (Phases 3–4) does not depend on this decision.** I can build and benchmark STT, LLM, TTS, and VAD in Colab with a **browser test client** that streams microphone audio over WebSocket through a Cloudflare Tunnel (HTTPS, which the tunnel supports). The same voice-engine interface later connects to the SIP side.
- **Production telephony requires one small always-on public host** (VPS) for SIP and RTP, under either option A or B. Colab cannot be it. That host runs only SIP/media (Asterisk or Kamailio+RTPEngine), not a business backend, so it stays inside your architecture rules.
- n8n, PostgreSQL, and the multi-tenant schema are unaffected.
