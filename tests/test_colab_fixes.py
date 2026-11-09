"""Regressions from the first live Colab test (2026-11-08)."""
from voice_engine.config import LlmCfg, TtsCfg
from voice_engine.llm import LlmClient
from voice_engine.prompts import with_language_hint


async def test_llm_retries_when_server_drops_connection(fake_llm):
    """Every reply after a tool call failed with 'Server disconnected'."""
    fake_llm.drop_next = 1
    fake_llm.scripts = [[{"text": "Your order is on the way. "}]]
    c = LlmClient(LlmCfg(base_url=fake_llm.base_url))
    text = "".join([e.text async for e in c.stream([{"role": "user", "content": "x"}]) if e.kind == "text"])
    await c.close()
    assert text == "Your order is on the way. "


def test_language_hint_on_last_user_message_only():
    """English questions were answered in Urdu after Urdu turns."""
    history = [{"role": "system", "content": "S"},
               {"role": "user", "content": "میرا آرڈر"}, {"role": "assistant", "content": "جی"},
               {"role": "user", "content": "Can you check my order?"},
               {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
               {"role": "tool", "tool_call_id": "c1", "content": "{}"}]
    out = with_language_hint(history, "en")
    assert out[3]["content"].startswith("[The caller is speaking English: reply in English.]")
    assert out[1]["content"] == "میرا آرڈر"                 # older turns untouched
    assert history[3]["content"] == "Can you check my order?"   # stored history not modified
    assert with_language_hint(history, None) is history


def test_mms_uses_a_separate_english_model(monkeypatch):
    """English text was sent to the Urdu-only MMS model and came out silent."""
    import voice_engine.tts as tts
    seen = []
    monkeypatch.setattr(tts, "MmsTts", lambda cfg, path: seen.append(str(path).replace("\\", "/")) or object())
    cfg = TtsCfg()
    tts.build_engine("mms", cfg, lambda p: p, "en")
    tts.build_engine("mms", cfg, lambda p: p, "ur")
    assert seen == ["models/tts/mms-tts-eng", "models/tts/mms-tts-urd-script_arabic"]
