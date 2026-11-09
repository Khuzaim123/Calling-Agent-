import pytest

from voice_engine.config import load_config

from conftest import ROOT


def test_repo_config_loads_and_env_overrides():
    cfg = load_config(ROOT / "config/voice_engine.yaml",
                      env={"VE__STT__BEAM_SIZE": "3", "VE__SERVER__SERVE_TEST_CLIENT": "false",
                           "VE__STT__ALLOWED_LANGUAGES": "en,ur", "VOICE_AGENT_ROOT": "/content/drive/x"})
    assert cfg.stt.beam_size == 3 and cfg.server.serve_test_client is False
    assert cfg.stt.allowed_languages == ["en", "ur"]
    assert str(cfg.path("models/a")).replace("\\", "/") == "/content/drive/x/models/a"
    assert cfg.tts.engine_by_language == {"en": "kokoro", "ur": "mms"}


def test_code_files_resolve_to_code_dir_not_data_root(tmp_path):
    """Colab layout: data root on Drive, code elsewhere. Regression for the dev_business.yaml crash."""
    from voice_engine.server import build_gateway
    cfg = load_config(ROOT / "config/voice_engine.yaml", env={"VOICE_AGENT_ROOT": str(tmp_path / "drive")})
    assert cfg.code_path("config/dev_business.yaml") == ROOT / "config/dev_business.yaml"
    assert str(cfg.path("logs/x")).startswith(str(tmp_path / "drive"))
    gw = build_gateway(cfg)                       # must find the YAML although project_root has no config/
    assert gw.data["business"]["id"] == "dev-0001"


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("stt:\n  beem_size: 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stt.beem_size"):
        load_config(p, env={})
