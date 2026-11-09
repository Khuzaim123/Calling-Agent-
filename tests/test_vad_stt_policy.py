import numpy as np

from voice_engine.config import SttCfg, VadCfg
from voice_engine.stt import choose_language, should_reject
from voice_engine.vad import CHUNK, CHUNK_MS, Endpointer


def run(ep, probs):
    events = []
    for i, p in enumerate(probs):
        chunk = np.full(CHUNK, i, dtype=np.float32)   # chunk value encodes its index
        events += ep.push(chunk, p)
    return events


def cfg(**kw):
    c = VadCfg(threshold=0.5, neg_threshold=0.35, min_speech_ms=96, end_silence_ms=320,
               preroll_ms=96, max_utterance_s=5.0, barge_in_min_speech_ms=200)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_short_blip_is_ignored():
    ev = run(Endpointer(cfg()), [0.0] * 5 + [0.9, 0.9] + [0.0] * 20)   # 64 ms < 96 ms
    assert not any(e.kind == "speech_start" for e in ev)


def test_utterance_with_preroll_and_end_silence():
    probs = [0.0] * 5 + [0.9] * 10 + [0.1] * 12
    ev = run(Endpointer(cfg()), probs)
    kinds = [e.kind for e in ev]
    assert kinds.count("speech_start") == 1 and kinds.count("utterance") == 1
    utt = next(e for e in ev if e.kind == "utterance")
    first_idx = int(utt.audio[0])
    assert first_idx == 2, "preroll (3 chunks = 96 ms) before the first speech chunk at index 5"
    n_chunks = len(utt.audio) // CHUNK
    # utterance ends exactly when end_silence_ms (10 chunks of 32 ms) of silence has accumulated
    assert int(utt.audio[-1]) == 5 + 10 + int(np.ceil(320 / CHUNK_MS)) - 1
    assert n_chunks == 3 + 10 + 10


def test_hysteresis_keeps_speech_between_thresholds():
    probs = [0.9] * 5 + [0.4] * 30 + [0.1] * 12          # 0.4 is between neg_threshold and threshold
    ev = run(Endpointer(cfg()), probs)
    utts = [e for e in ev if e.kind == "utterance"]
    assert len(utts) == 1 and len(utts[0].audio) // CHUNK >= 35


def test_forced_cut_at_max_utterance():
    ev = run(Endpointer(cfg(max_utterance_s=1.0)), [0.9] * 100)
    utts = [e for e in ev if e.kind == "utterance"]
    assert utts and all(u.forced for u in utts)


def test_speech_ms_grows_for_barge_in():
    ev = run(Endpointer(cfg()), [0.9] * 12)
    speech = [e.speech_ms for e in ev if e.kind == "speech"]
    assert speech == sorted(speech) and speech[-1] >= 200


def test_choose_language_folds_hindi_into_urdu():
    lang, p = choose_language({"hi": 0.55, "ur": 0.25, "en": 0.1}, ["en", "ur"], None)
    assert lang == "ur" and abs(p - 0.8) < 1e-9


def test_choose_language_respects_allowed_and_fallback():
    assert choose_language({"ar": 0.9}, ["en", "ur"], "ur")[0] == "ur"
    assert choose_language({"ar": 0.9}, ["en", "ur"], None)[0] == "en"
    assert choose_language({"en": 0.7, "ur": 0.2}, ["en", "ur"], "ur")[0] == "en"


def test_should_reject():
    c = SttCfg()
    assert should_reject("", -0.2, 0.0, c) == "empty"
    assert should_reject("Thank you.", -1.5, 0.9, c) == "no_speech"
    assert should_reject("order 4821", -0.3, 0.9, c) == ""
