"""Configuration loading: YAML file + environment overrides.

Secrets are never stored in YAML. YAML fields ending in `_env` name the
environment variable that holds the secret; `secret()` resolves them.
Override any scalar with VE__SECTION__KEY=value (e.g. VE__STT__BEAM_SIZE=2).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

CODE_DIR = Path(__file__).resolve().parent.parent      # repository root (voice_engine/..)


@dataclass
class ServerCfg:
    host: str = "127.0.0.1"
    port: int = 8765
    ws_token_env: str = "VOICE_ENGINE_WS_TOKEN"
    max_sessions: int = 2
    max_session_seconds: int = 900
    max_frame_bytes: int = 8192          # one inbound WS binary message
    max_frames_per_second: int = 100     # flood protection (20 ms frames => 50/s)
    serve_test_client: bool = True


@dataclass
class VadCfg:
    threshold: float = 0.5
    neg_threshold: float = 0.35          # hysteresis: speech ends below this
    min_speech_ms: int = 160
    end_silence_ms: int = 650
    preroll_ms: int = 300
    max_utterance_s: float = 20.0
    barge_in_min_speech_ms: int = 280


@dataclass
class SttCfg:
    model_path: str = "models/stt/faster-whisper-large-v3-turbo"
    device: str = "cuda"
    compute_type: str = "int8_float16"
    beam_size: int = 1
    default_language: str = "auto"       # "auto" | "en" | "ur"
    allowed_languages: list[str] = field(default_factory=lambda: ["en", "ur"])
    no_speech_threshold: float = 0.6
    min_avg_logprob: float = -1.2


@dataclass
class LlmCfg:
    base_url: str = "http://127.0.0.1:8081/v1"
    model: str = "local"
    api_key_env: str = "LLM_API_KEY"
    max_tokens: int = 220
    temperature: float = 0.4
    timeout_s: float = 30.0
    max_tool_rounds: int = 3
    history_char_budget: int = 6000
    chat_template_kwargs: dict[str, Any] = field(default_factory=lambda: {"enable_thinking": False})


@dataclass
class TtsCfg:
    engine_by_language: dict[str, str] = field(default_factory=lambda: {"en": "kokoro", "ur": "mms"})
    device: str = "cuda"
    kokoro_voice: str = "af_heart"
    kokoro_lang_code: str = "a"
    kokoro_repo: str = "hexgrad/Kokoro-82M"
    mms_model_path: str = "models/tts/mms-tts-urd-script_arabic"
    mms_model_path_en: str = "models/tts/mms-tts-eng"      # English fallback when kokoro can't run (Python 3.13)
    azure_region: str = ""
    azure_key_env: str = "AZURE_SPEECH_KEY"
    azure_voice_ur: str = "ur-PK-UzmaNeural"
    azure_voice_en: str = "en-US-JennyNeural"


@dataclass
class BusinessCfg:
    gateway: str = "static"               # "static" (dev fixtures) | "n8n"
    static_file: str = "config/dev_business.yaml"
    n8n_base_url: str = "http://127.0.0.1:5678/webhook"
    hmac_secret_env: str = "N8N_WEBHOOK_HMAC_SECRET"
    timeout_s: float = 8.0
    retries: int = 2


@dataclass
class OutputCfg:
    browser_rate: int = 24000


@dataclass
class TelephonyCfg:
    enabled: bool = False                 # phone channel + gateway control + /calls API
    gateway_token_env: str = "VOICE_ENGINE_GATEWAY_TOKEN"
    calls_token_env: str = "VOICE_ENGINE_CALLS_TOKEN"
    phone_rate: int = 16000               # PCM rate on the gateway link (PipeWire resamples CVSD/mSBC)


@dataclass
class LogCfg:
    dir: str = "logs/voice_engine"
    level: str = "INFO"


@dataclass
class Config:
    project_root: str = "."
    server: ServerCfg = field(default_factory=ServerCfg)
    vad: VadCfg = field(default_factory=VadCfg)
    stt: SttCfg = field(default_factory=SttCfg)
    llm: LlmCfg = field(default_factory=LlmCfg)
    tts: TtsCfg = field(default_factory=TtsCfg)
    business: BusinessCfg = field(default_factory=BusinessCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    telephony: TelephonyCfg = field(default_factory=TelephonyCfg)
    log: LogCfg = field(default_factory=LogCfg)

    def path(self, p: str) -> Path:
        """Resolve a DATA path (models/, logs/) relative to project_root (absolute paths pass through)."""
        pp = Path(p)
        return pp if pp.is_absolute() else Path(self.project_root) / pp

    @staticmethod
    def code_path(p: str) -> Path:
        """Resolve a path to a file shipped with the code (config/*.yaml) relative to the code directory.

        In Colab the code lives in /content/runtime/code while project_root is the Drive folder."""
        pp = Path(p)
        return pp if pp.is_absolute() else CODE_DIR / pp


def secret(env_name: str, required: bool = True) -> str:
    val = os.environ.get(env_name, "")
    if required and not val:
        raise RuntimeError(f"Required secret environment variable {env_name} is not set")
    return val


def _coerce(value: str, target: Any) -> Any:
    if isinstance(target, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(target, int):
        return int(value)
    if isinstance(target, float):
        return float(value)
    if isinstance(target, list):
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def _apply(obj: Any, data: dict, where: str) -> None:
    known = {f.name for f in fields(obj)}
    for k, v in (data or {}).items():
        if k not in known:
            raise ValueError(f"Unknown config key {where}{k}")
        cur = getattr(obj, k)
        if is_dataclass(cur):
            if not isinstance(v, dict):
                raise ValueError(f"Config {where}{k} must be a mapping")
            _apply(cur, v, f"{where}{k}.")
        else:
            setattr(obj, k, v)


def load_config(path: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    cfg = Config()
    if path:
        with open(path, encoding="utf-8") as fh:
            _apply(cfg, yaml.safe_load(fh) or {}, "")
    for key, value in env.items():
        if not key.startswith("VE__"):
            continue
        parts = key[4:].lower().split("__")
        obj: Any = cfg
        for p in parts[:-1]:
            obj = getattr(obj, p)
        cur = getattr(obj, parts[-1])
        setattr(obj, parts[-1], _coerce(value, cur))
    if env.get("VOICE_AGENT_ROOT"):
        cfg.project_root = env["VOICE_AGENT_ROOT"]
    return cfg
