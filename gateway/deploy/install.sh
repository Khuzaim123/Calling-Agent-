#!/usr/bin/env bash
# Install the HFP gateway on Raspberry Pi OS / Debian 13 "trixie" (bluez 5.82, ofono 2.16,
# pipewire 1.4.2, wireplumber 0.5.8 in the archive as of 2026-11-07).
# Run as your normal user (needs sudo), from the repository root:  bash gateway/deploy/install.sh
set -euo pipefail

if [[ $EUID -eq 0 ]]; then echo "run as your normal user, not root"; exit 1; fi
USER_NAME="$(id -un)"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
. /etc/os-release
echo "OS: $PRETTY_NAME"
[[ "${VERSION_CODENAME:-}" == "trixie" ]] || echo "WARNING: tested configuration targets Debian 13 trixie; WirePlumber < 0.5 uses a different config format."

sudo apt-get update -qq
sudo apt-get install -y bluez ofono pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth \
     pulseaudio-utils python3-venv
if dpkg -s pulseaudio >/dev/null 2>&1; then
  echo "WARNING: the 'pulseaudio' daemon is installed and conflicts with pipewire-pulse; remove it: sudo apt purge pulseaudio"
fi

# oFono's default D-Bus policy only allows root and the console user. The gateway and
# PipeWire's oFono backend run as $USER_NAME in a headless user session.
sed "s/__USER__/${USER_NAME}/" "$REPO/gateway/deploy/voice-gateway-ofono.conf" | \
  sudo tee /etc/dbus-1/system.d/voice-gateway-ofono.conf >/dev/null
sudo systemctl reload dbus || true

# WirePlumber: act as HFP Hands-Free (the phone is the Audio Gateway), oFono backend.
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
cp "$REPO/gateway/deploy/51-voice-gateway-hfp.conf" ~/.config/wireplumber/wireplumber.conf.d/

sudo systemctl enable --now bluetooth ofono
sudo systemctl restart ofono
sudo loginctl enable-linger "$USER_NAME"          # user services (PipeWire, gateway) run without a login
systemctl --user enable --now pipewire pipewire-pulse wireplumber
systemctl --user restart wireplumber

python3 -m venv ~/.local/share/voice-gateway/venv
~/.local/share/voice-gateway/venv/bin/pip install -q -r "$REPO/gateway/requirements.txt"

mkdir -p ~/.config/voice-gateway ~/.config/systemd/user
[[ -f ~/.config/voice-gateway/gateway.yaml ]] || cp "$REPO/gateway/config.example.yaml" ~/.config/voice-gateway/gateway.yaml
if [[ ! -f ~/.config/voice-gateway/env ]]; then
  umask 077
  printf 'VOICE_ENGINE_GATEWAY_TOKEN=\nVOICE_GATEWAY_ENGINE_URL=\n' > ~/.config/voice-gateway/env
fi
chmod 600 ~/.config/voice-gateway/env
sed "s|__REPO__|${REPO}|" "$REPO/gateway/deploy/voice-gateway.service" > ~/.config/systemd/user/voice-gateway.service
systemctl --user daemon-reload

cat <<EOF

Installed. Next steps (docs/telephony-redmi9c-gateway.md):
  1. Pair the Redmi 9C:   bluetoothctl  (see the doc for the exact sequence)
  2. Edit ~/.config/voice-gateway/gateway.yaml  (phone_bt_address, allowlists)
  3. Put the token in      ~/.config/voice-gateway/env
  4. Check:                ~/.local/share/voice-gateway/venv/bin/python -m gateway check --config ~/.config/voice-gateway/gateway.yaml
  5. Telephony test:       ... -m gateway dial-test --number <your second phone> --config ...
  6. Start the service:    systemctl --user enable --now voice-gateway
EOF
