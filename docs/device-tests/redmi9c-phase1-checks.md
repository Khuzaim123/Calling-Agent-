# Redmi 9C: Phase 1 On-Device Checks

> **Primary telephony tests are now G1–G10 in `docs/telephony-redmi9c-gateway.md`** (Bluetooth HFP gateway).
> T1–T4 below are still useful evidence for the record. T3/T4 measure the on-phone *acoustic* fallback
> (option C) and confirm *why* an app cannot be the gateway.

These four tests settle the open questions in `docs/phase1-rappelo-audit.md`.
None of them requires building Rappelo. T1 and T2 need `adb` once (USB debugging on, any computer). T3 and T4 need only the phone and a second phone.

Record the actual results in the table at the bottom. **Do not mark a test passed unless you ran it.**

## Recommended: the guided harness
`tools/device/redmi9c_checks.py` runs T1 and T2 automatically and guides you through T3 and T4 with a
second phone. It captures Android's own audio-policy state *during* the call (`dumpsys audio`,
`dumpsys media.audio_policy`) and tells you whether the recording app is a system or third-party app. For T4
it plays a countable, speech-like test signal; pure tones are a bad test because noise suppression removes them.

```powershell
cd "D:\Programing\Demo projects\Calling agent"
python tools\device\redmi9c_checks.py              # T1-T4, guided (~15 min, needs a second phone)
python tools\device\redmi9c_checks.py --only t1,t2 # automatic part only (~10 s)
```
Results go to `docs/device-tests/results/redmi9c_<timestamp>/summary.json` (plus raw dumps). Send me
`summary.json`. The raw dumps list your installed apps, so review them before sharing. Your answers to the
listening questions are stored as `user_reported`; the script never turns them into a pass by itself.
The manual steps below are the same tests without the script.

## Setup (T1, T2 only)
1. Settings → About phone → tap **MIUI version** 7 times to enable Developer options.
2. Settings → Additional settings → Developer options → enable **USB debugging**.
3. Connect USB and accept the RSA prompt on the phone.

## T1: CPU ABI (is the arm64-only PJSIP usable?)
```bash
adb shell getprop ro.product.cpu.abilist
adb shell getprop ro.product.cpu.abi
adb shell getprop ro.build.version.release
adb shell getprop ro.miui.ui.version.name
```
- Output contains `arm64-v8a`: the shipped PJSIP AAR can load.
- Output is only `armeabi-v7a,armeabi`: **Rappelo's APK cannot run as shipped** (PJSIP must be rebuilt for 32-bit).

## T2: Protection level of the call-audio permissions on this firmware
```bash
adb shell pm list permissions -f | grep -A4 -E "CAPTURE_AUDIO_OUTPUT|MODIFY_PHONE_STATE"
```
Expected: `protectionLevel:signature|privileged` for both. Any other value is a surprise worth reporting.

## T3: Can an ordinary app record the microphone during a cellular call?
Use a **third-party** voice recorder from the Play Store, not MIUI's preinstalled Recorder, which is a system app and would not tell us anything.
1. Call the Redmi 9C from a second phone and answer.
2. Turn on **speakerphone**.
3. Open the recorder app and record 20 seconds while the other person speaks and you speak.
4. Hang up and play the recording back.

| Result | Meaning |
|---|---|
| Silence (or only before/after the call) | Confirms the Android 10 capture policy: Rappelo as written gets no caller audio. |
| Caller audible | MIUI deviates from the AOSP policy. Note the quality: clear, noisy, distant? |

Optional T3b: repeat with a call-recorder app that uses an **accessibility service** (Android 10+ call recorders typically ask you to enable one). This is the best audio a non-root app can get, so judge its quality as if a customer were on the line.

## T4: Does speaker playback reach the far end during a call?
This is the AI-voice injection path.
1. Call the Redmi 9C from a second phone, answer, and turn on **speakerphone**.
2. On the Redmi, play a spoken-word audio clip (a podcast or YouTube video) at full media volume. Stay silent yourself.
3. Ask the person on the second phone: do they hear it? Clearly, faintly, choppy, or not at all?

| Result | Meaning |
|---|---|
| Not heard / fades out after a second | The modem echo canceller removes speaker output from the uplink: the AI voice cannot reach callers. |
| Heard but faint or garbled | The acoustic path works, at unusable quality. |
| Heard clearly | The acoustic path is usable for experiments (still not commercial). |

## Results
| Test | Date | Result (paste output or describe) | Pass/Fail |
|---|---|---|---|
| T1 ABI | | | |
| T2 perms | | | |
| T3 in-call capture | | | |
| T3b accessibility recorder | | | |
| T4 speaker injection | | | |
