import numpy as np

from voice_engine.audio import (FrameAssembler, float_to_pcm16, mulaw_decode, mulaw_encode, pcm16_to_float,
                                resample, simulate_phone_channel)
from voice_engine.text import (SentenceStreamer, dominant_script_language, normalize_for_scoring,
                               script_profile, strip_reasoning)


def test_pcm_roundtrip():
    x = np.linspace(-0.9, 0.9, 1000, dtype=np.float32)
    y = pcm16_to_float(float_to_pcm16(x))
    assert np.max(np.abs(x - y)) < 1e-4


def test_resample_lengths():
    x = np.zeros(24000, np.float32)
    assert len(resample(x, 24000, 16000)) == 16000
    assert len(resample(x, 24000, 8000)) == 8000
    assert len(resample(np.zeros(16000, np.float32), 16000, 24000)) == 24000


def test_mulaw_roundtrip_error_small():
    t = np.arange(8000) / 8000
    x = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    y = mulaw_decode(mulaw_encode(x))
    snr = 10 * np.log10(np.sum(x ** 2) / np.sum((x - y) ** 2))
    assert snr > 30, snr  # 8-bit mu-law on a mid-level tone


def test_phone_channel_removes_high_band():
    t = np.arange(16000) / 16000
    hi = np.sin(2 * np.pi * 6000 * t).astype(np.float32) * 0.5   # above 4 kHz Nyquist of 8 kHz
    out = simulate_phone_channel(hi)
    assert len(out) == len(hi)
    assert np.sqrt(np.mean(out ** 2)) < 0.05


def test_frame_assembler():
    fa = FrameAssembler(512)
    assert fa.push(b"\x00" * 1000) == []
    frames = fa.push(b"\x00" * 100)
    assert len(frames) == 1 and len(frames[0]) == 1024


def test_script_detection():
    assert dominant_script_language("آپ کا آرڈر راستے میں ہے") == "ur"
    assert dominant_script_language("Your order is on the way") == "en"
    assert dominant_script_language("آپ کا order راستے میں ہے") == "ur"
    p = script_profile("aap ka order rastay mein hai")       # Roman Urdu is Latin script
    assert p["latin"] == 1.0 and p["arabic"] == 0.0


def test_urdu_normalization():
    # Arabic yeh/kaf/heh variants and diacritics normalise to Urdu forms
    a = normalize_for_scoring("كيا آپ نے اَرڈر كيا؟", "ur")
    b = normalize_for_scoring("کیا آپ نے ارڈر کیا", "ur")
    assert a == b
    assert normalize_for_scoring("Hello, World!", "en") == "hello world"


def test_sentence_streamer_english():
    s = SentenceStreamer()
    out = []
    for tok in ["Your order ", "is on the way. ", "It costs 3.5", " thousand rupees. Thanks"]:
        out += s.push(tok)
    out += s.flush()
    assert out == ["Your order is on the way.", "It costs 3.5 thousand rupees.", "Thanks"]


def test_sentence_streamer_urdu_full_stop():
    s = SentenceStreamer()
    out = s.push("آپ کا آرڈر راستے میں ہے۔ ")
    out += s.push("پندرہ منٹ لگیں گے۔")
    out += s.flush()
    assert out == ["آپ کا آرڈر راستے میں ہے۔", "پندرہ منٹ لگیں گے۔"]


def test_sentence_streamer_first_soft_split():
    s = SentenceStreamer(first_soft_min_chars=20)
    out = s.push("Thank you for calling Karachi Pizza, let me check that")
    assert out == ["Thank you for calling Karachi Pizza,"]
    assert s.push(", one moment") == []        # soft split only applies to the first chunk


def test_strip_reasoning():
    assert strip_reasoning("<think>plan</think>Hello") == "Hello"
