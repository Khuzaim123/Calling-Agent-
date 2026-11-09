"""Gateway configuration (YAML) + secrets from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from .policy import PolicyCfg


@dataclass
class GatewayCfg:
    engine_url: str = ""                      # https://<tunnel host> of the voice engine (Colab)
    token_env: str = "VOICE_ENGINE_GATEWAY_TOKEN"
    line_name: str = "redmi9c-ufone"
    phone_bt_address: str = ""                # AA:BB:CC:DD:EE:FF of the Redmi 9C
    rate: int = 16000                         # must equal the engine's telephony.phone_rate
    frame_ms: int = 20
    playout_latency_ms: int = 60
    audio_wait_s: float = 8.0                 # wait for SCO audio nodes after a call goes active
    state_file: str = "~/.local/state/voice-gateway/policy.json"
    recordings_dir: str = "~/voice-gateway-tests"
    policy: PolicyCfg = field(default_factory=PolicyCfg)

    @property
    def token(self) -> str:
        v = os.environ.get(self.token_env, "")
        if len(v) < 24:
            raise RuntimeError(f"{self.token_env} is missing or shorter than 24 characters")
        return v

    def ws_url(self, path: str) -> str:
        base = self.engine_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[8:]
        elif base.startswith("http://"):
            base = "ws://" + base[7:]
        return base + path


def load(path: str | os.PathLike) -> GatewayCfg:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = GatewayCfg()
    known = {f.name for f in fields(GatewayCfg)}
    for k, v in data.items():
        if k not in known:
            raise ValueError(f"unknown gateway config key: {k}")
        if k == "policy":
            pk = {f.name for f in fields(PolicyCfg)}
            bad = set(v) - pk
            if bad:
                raise ValueError(f"unknown policy keys: {sorted(bad)}")
            cfg.policy = PolicyCfg(**v)
        else:
            setattr(cfg, k, v)
    cfg.state_file = str(Path(cfg.state_file).expanduser())
    cfg.recordings_dir = str(Path(cfg.recordings_dir).expanduser())
    if os.environ.get("VOICE_GATEWAY_ENGINE_URL"):
        cfg.engine_url = os.environ["VOICE_GATEWAY_ENGINE_URL"]   # quick-tunnel URL changes per Colab session
    return cfg
