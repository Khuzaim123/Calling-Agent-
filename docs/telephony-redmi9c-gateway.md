# Telephony: Redmi 9C + Ufone SIM as the GSM gateway (personal deployment)

Scope: **one personal deployment**, your own Redmi 9C (M2006C3MG, Android 10, MIUI 12.0.23, unrooted) and
your own Ufone SIM. No paid telephony. Status date 2026-11-07. **Nothing in this document has been tested on
your phone yet.** The status table at the end says exactly what has and hasn't run.

## 1. The critical question

> Can an ordinary Android 10 app on an unrooted Redmi 9C place and receive Ufone calls **and** expose their
> real bidirectional audio to an external AI agent?

**No, not through an app.** This is Android policy, not a Rappelo bug:

| Capability for a user-installed app | Android 10 without root | Evidence |
|---|---|---|
| Place a SIM call | **Yes**, `TelecomManager.placeCall` with `CALL_PHONE` | Rappelo code (Phase 1) |
| Answer, reject, hang up | **Yes**, as the default phone app (`InCallService`) | Rappelo code |
| Read the call audio (downlink/uplink) | **No.** Needs `CAPTURE_AUDIO_OUTPUT` (signature/privileged). An ordinary app's microphone capture is **silenced** during a call; only accessibility services and privileged apps are exempt. | [Android: sharing audio input](https://developer.android.com/media/platform/sharing-audio-input) |
| Inject audio into the call uplink | **No.** There is no API. The only route is the loudspeaker into the microphone (acoustic coupling). | Phase 1 audit |

**But Android does give full two-way call audio to one class of device: a hands-free headset.** Every phone
implements the Bluetooth **Hands-Free Profile (HFP)** Audio Gateway role, the same thing a car kit uses. When an
HFP hands-free unit is connected, the phone sends it caller ID and ring indications, accepts dial, answer and hang-up
commands from it, and routes **both directions of call audio** to it over the Bluetooth SCO link. No app and no
permission on the phone is involved, and the phone's SIM remains the carrier.

A small Linux machine can play the hands-free role: BlueZ for Bluetooth, **oFono** for HFP call control,
**PipeWire** for SCO audio. This setup is documented in practice: Sipfront (2025) uses a Raspberry Pi with
bluetoothd + oFono + PipeWire/WirePlumber to place calls on real phones and capture their audio
([Sipfront blog](https://sipfront.com/blog/2025/03/automating-calls-on-real-mobile-devices-with-bluetooth-hfp-on-a-raspberry-pi/)).
WirePlumber lists `hfp_hf` (Hands-Free) as a role and `ofono` as an HFP backend
([WirePlumber Bluetooth config](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/bluetooth.html)).
Car-kit projects do the same ([phony](https://github.com/littlecraft/phony),
[raspberry-link](https://github.com/alejzeis/raspberry-link)).

**What this does not prove:** that *your* Redmi 9C + MIUI + the specific Bluetooth adapter behave correctly. In
particular, incoming-call answering with two-way audio isn't shown in the Sipfront article, and the phony project
reports the Raspberry Pi 3's *onboard* Bluetooth misbehaving with oFono HFP. Tests G1–G8 below settle this.

## 2. SIP server vs. GSM-to-SIP gateway (investigated separately)

| Question | Answer | Why |
|---|---|---|
| Can the phone be a **GSM-to-SIP gateway** (SIM call ↔ SIP/RTP audio) by itself? | **No** without root | It would need call audio, which apps can't access (section 1). An acoustic app is the only on-phone option (section 6, option C). |
| Can the phone **host a SIP server** (Kamailio, Asterisk, RTPEngine)? | **Not practically** | None of asterisk, kamailio, opensips, freeswitch, baresip, pjsip, rtpengine or coturn exist as Termux packages (all checked 2026-11-07: 404). You'd have to cross-compile, keep the process alive against MIUI's background killing, and it **still couldn't touch call audio**. |
| Is SIP needed anywhere in this deployment? | **No** | Nothing in the chain speaks SIP. The voice engine runs in Colab, which has no inbound UDP, so SIP/RTP can't reach it anyway. The gateway streams PCM over the voice engine's **existing** authenticated WebSocket protocol through the Cloudflare tunnel (HTTPS, outbound from your home, no port forwarding). SIP would add Kamailio + RTPEngine + a public UDP host without carrying any extra capability. |

If SIP is ever wanted (e.g., to attach a desk phone), the gateway box could run Asterisk on your LAN. That's optional
future work and isn't implemented.

## 3. Architecture

```
                   ┌──────────────────────── Google Colab (T4) ─────────────────────────┐
                   │  n8n ──POST /calls (Bearer)──►┐            PostgreSQL (127.0.0.1)    │
                   │   ▲  call events / session     │                    ▲                 │
                   │   │  start/end, tools (HMAC)   ▼                    │ (n8n only)      │
                   │   └──────────────── voice_engine (aiohttp :8765) ───┘                 │
                   │        VAD ► STT ► LLM(llama-server) ► TTS, barge-in                   │
                   │        telephony.py: line state, dial/hangup relay (no business rules) │
                   └──────────────▲───────────────────────────────────────────────────────┘
                                  │ wss://<tunnel>/ws/gateway  (control: dial, hangup, call_status)
                                  │ wss://<tunnel>/ws          (per call: PCM16 16 kHz both ways)
                                  │ cloudflared quick/named tunnel (HTTPS only)
   your home ─────────────────────┼──────────────────────────────────────────────────────────
                   ┌──────────────┴──── gateway box (Raspberry Pi / any Linux + Bluetooth) ──┐
                   │  gateway/ (python -m gateway run, systemd --user)                       │
                   │   policy (allowlists, limits) ─ bridge ─┬─ oFono D-Bus: Dial/Answer/Hangup│
                   │                                         └─ PipeWire: parec / pacat (SCO) │
                   └──────────────────────────────▲──────────────────────────────────────────┘
                                                  │ Bluetooth HFP (AG = phone, HF = gateway box)
                                                  │ call control (AT commands) + SCO audio (CVSD 8k / mSBC 16k)
                   ┌──────────────────────────────┴───────┐
                   │ Redmi 9C, Ufone SIM, NO app, NO root │ ◄──── cellular network ────► the other party
                   └──────────────────────────────────────┘
```

**What each piece does**
- **Redmi 9C:** SIM, GSM/VoLTE registration, dialing, ringing, caller ID, call audio codec. Everything a phone does natively.
- **Gateway box:** the only new infrastructure. It converts HFP into the voice engine's WebSocket protocol and enforces
  the **SIM safety policy**:
  - outbound allowlist, hourly dial and daily-minute limits;
  - premium numbers (0900) never dialled;
  - inbound allowlist with a ring delay, so you can still answer your own calls;
  - calls from anyone not on the allowlist never leave the box.
- **Voice engine:** unchanged pipeline. It gains a `gsm` channel, the gateway control link and a small call API for n8n
  (`voice_engine/telephony.py`). It stores no business data.
- **n8n:** decides whom to call and why (`POST /calls` with an opaque `context`), and receives call status events
  (`/voice/call-event`) and session start/end as before. The workflows are built in Phase 6. **None exist yet**, so none
  had to change.
- **PostgreSQL:** unchanged. Call records are written by n8n from call events (Phase 6 schema).

**Why the gateway box is necessary:** call audio leaves an unrooted phone only through a headset interface
(Bluetooth HFP or the 3.5 mm jack). Both need a physical device within metres of the phone. Colab is remote and
has no Bluetooth, and your laptop must not be required. A Raspberry Pi-class box is the smallest thing that can do
it. It's hardware you own, not a subscription.

## 4. Hardware and software requirements

- Any Linux box with Bluetooth that can stay on near the phone, for example a Raspberry Pi 4/5.
  **Use a USB Bluetooth dongle if the onboard adapter misbehaves.** The phony project reports Pi 3 onboard Bluetooth
  failing with oFono HFP. Which adapters work with your phone is **unverified**.
- OS: Raspberry Pi OS / Debian 13 "trixie", which ships bluez 5.82, ofono 2.16, pipewire 1.4.2 and wireplumber 0.5.8 (Debian
  archive, checked 2026-11-07). Older WirePlumber (0.4) uses a different configuration format that is not provided here.
- The Redmi 9C on a charger, within Bluetooth range (~10 m line of sight), with Ufone balance or a call package.

## 5. Installation and configuration

### 5.1 Gateway box
```bash
git clone <your repo> ~/voice-agent && cd ~/voice-agent      # or copy the folder
bash gateway/deploy/install.sh
```
This installs bluez/ofono/pipewire, and sets WirePlumber to `bluez5.roles = [ hfp_hf ]` with the oFono backend
(`gateway/deploy/51-voice-gateway-hfp.conf`). It also adds a D-Bus policy so your user may talk to oFono
(`voice-gateway-ofono.conf`), creates a Python venv, and installs a systemd *user* service with lingering enabled.

### 5.2 Pair the Redmi 9C (once)
On the gateway box:
```bash
bluetoothctl
  power on
  agent NoInputNoOutput
  default-agent
  discoverable on
  pairable on
```
On the phone: **Settings → Bluetooth → turn on → tap the gateway box's name** (its hostname) and confirm pairing.
Then, back in `bluetoothctl`:
```bash
  devices                      # find the phone's address
  trust AA:BB:CC:DD:EE:FF
  connect AA:BB:CC:DD:EE:FF
  info AA:BB:CC:DD:EE:FF       # expect "Connected: yes" and "Handsfree Audio Gateway" among the UUIDs
  discoverable off
  quit
```
On the phone, tap the ⚙ next to the paired device: keep **Phone calls** on and turn **Media audio** off. If asked
to share contacts or call history, you can refuse; caller ID still works without it.

**Important:** while the phone is connected, *every* call's audio (including your personal calls) is routed to
the gateway box by default. Calls from numbers outside `inbound_allowlist` are never answered by the gateway or
reported anywhere, but if you answer one on the phone, tap the audio-route button (Bluetooth → Phone) in the call
screen to hear it on the handset, or switch off **Phone calls** for the device when you don't want the gateway.

### 5.3 Configure
`~/.config/voice-gateway/gateway.yaml` (copied from `gateway/config.example.yaml`):
- `phone_bt_address`: from `bluetoothctl devices`.
- `outbound_allowlist` / `inbound_allowlist`: start with **only your second test phone**.
- Limits: `max_outbound_per_hour`, `max_outbound_minutes_per_day`, `max_call_seconds`.

`~/.config/voice-gateway/env` (mode 600):
```
VOICE_ENGINE_GATEWAY_TOKEN=<from Colab Secrets or Drive secrets/generated.env>
VOICE_GATEWAY_ENGINE_URL=<public URL printed by notebook cell 17>
```
The quick-tunnel URL changes with every Colab session: update `VOICE_GATEWAY_ENGINE_URL` and run
`systemctl --user restart voice-gateway`. A named Cloudflare tunnel (`CLOUDFLARE_TUNNEL_TOKEN`) gives a stable hostname.

### 5.4 Colab side
`config/voice_engine.yaml` → `telephony.enabled: true` (default now). Run the notebook as before; cell 17 prints
the URL and the gateway env lines. Cell 17a shows the line status and can place a manual test call.

## 6. Alternatives that keep your Ufone SIM

| Option | Audio quality | Phone app | Extra hardware | Status |
|---|---|---|---|---|
| **A. Bluetooth HFP → Linux gateway** (implemented) | Line-level digital: CVSD 8 kHz narrowband, or mSBC 16 kHz if both sides support it | none | Linux box + Bluetooth | implemented, **untested on hardware** |
| B. 3.5 mm TRRS cable → USB sound card on the Linux box | Line-level analogue (needs level matching; ground-loop noise possible) | none for audio; call control via `adb` over USB/Wi-Fi or a default-dialer app | cable + USB sound card | design only; fallback if HFP fails on your phone |
| C. On-phone acoustic app (Rappelo approach) | Poor: speaker → air → mic, echo, AI hears itself | default dialer + accessibility service | none | not implemented; T3/T4 in `docs/device-tests/` measure it |
| D. Root / custom ROM (`CAPTURE_AUDIO_OUTPUT`) | Line-level | system app | none | excluded by your "unrooted" constraint |

## 7. Testing with your actual phone and SIM

Run these in order on the gateway box (`PY=~/.local/share/voice-gateway/venv/bin/python`,
`CFG=~/.config/voice-gateway/gateway.yaml`, from `~/voice-agent`). Each writes JSON/WAV to `~/voice-gateway-tests/`.

| # | Test | Command / action | Pass criteria (you judge, by measurement or ear) |
|---|---|---|---|
| G1 | Stack + phone readiness | `$PY -m gateway check --config $CFG` | every line `OK`, including "oFono HFP modem with VoiceCallManager" |
| G2 | **Outbound call, two-way audio, no AI** | `$PY -m gateway dial-test --number <second phone> --seconds 20 --config $CFG` | second phone rings and **shows your Ufone number**; after answering, it hears bursts in groups 2, 3, 5; `downlink_wav` contains the second phone's voice; downlink RMS clearly above silence |
| G3 | **Inbound call, two-way audio, no AI** | `$PY -m gateway answer-test --seconds 20 --config $CFG`, then call your Ufone number from the second phone | gateway answers after the ring delay; same audio checks as G2 |
| G4 | Codec | during G2: `wpctl status` / `pactl list short sinks` | note 8000 Hz (CVSD) or 16000 Hz (mSBC); record it |
| G5 | **Outbound AI call** | start the voice engine (cell 17) and the gateway service (`systemctl --user start voice-gateway`), then cell 17a with your second phone's number | phone rings, agent greets after you answer, holds a conversation, the call ends cleanly; statuses reach `ended` |
| G6 | **Inbound AI call** | call your Ufone number from the allowlisted second phone | AI answers after ~6 s, converses, hang-up is detected |
| G7 | Barge-in on a real call | talk over the agent during G5/G6 | agent stops within ~0.5 s |
| G8 | Personal calls untouched | call from a number **not** in `inbound_allowlist` | phone rings normally; gateway never answers; nothing appears in Colab logs |
| G9 | Screen off / long run | phone locked, screen off, 2 h idle, then repeat G6 | still works; note any MIUI Bluetooth disconnects in `journalctl --user -u voice-gateway` |
| G10 | Reconnect | turn Bluetooth off and on on the phone during idle | `modem_lost` then reconnect in the gateway log; G6 works again |

Send me the `~/voice-gateway-tests/*.json` files and your notes per test. The WAVs contain voices, so review them before sharing.

## 8. Status: implemented vs. verified

| Item | Implemented | Tested | Notes |
|---|---|---|---|
| Voice engine `gsm` channel, gateway control link, `/calls` API, call events | yes | **passed locally** (real engine + test doubles for phone/audio) | |
| Gateway policy (allowlists, limits, premium block, persistence) | yes | **passed locally** | |
| Gateway call state machine (dial, answer, timeout, hang-up both ways, handset-answered calls not bridged, personal calls not reported) | yes | **passed locally** | against a fake modem |
| PipeWire node selection | yes | passed locally on sample `pactl` output | real node names **unverified** |
| oFono D-Bus control (`gateway/ofono.py`) | yes | **not tested** | needs the box + phone (G1–G3) |
| SCO audio capture/playback (`gateway/audio.py`) | yes | **not tested** | G2/G3 |
| Install script, WirePlumber/D-Bus config, systemd unit | yes | **not tested** | |
| Redmi 9C dialing via HFP with Ufone caller ID | n/a (phone native) | **not tested** | G2 |
| Redmi 9C answering via HFP, two-way audio | n/a | **not tested** | G3; not shown in the cited sources |
| mSBC wideband on Redmi 9C | n/a | **unknown** | G4 |
| Screen-off / MIUI long-run stability of the HFP link | n/a | **unknown** | G9 |
| SIP server on the phone | no | n/a | not practical (section 2) |
| App-level call audio on unrooted Android 10 | no | n/a | **impossible** without system privileges |

## 9. Limitations that remain

- **One call at a time.** One SIM, one HFP link.
- **Narrowband** if the phone negotiates CVSD (8 kHz). STT benchmarks include a phone-channel condition for this.
- **Added latency:** Bluetooth SCO plus the tunnel round-trip to Colab, on top of the pipeline latency. Measure it in G5.
- **All of the phone's call audio is routed to the box while connected** (section 5.2).
- **Colab sessions end**, which takes the AI offline. The gateway then refuses to answer, so callers just ring
  through to the phone, and outbound requests fail. The quick-tunnel URL changes each session.
- **Your own SIM and number are used.** The policy limits protect your balance. The agent's greeting identifies
  itself as automated and mentions recording. Keep doing that, and only call people who expect it.
