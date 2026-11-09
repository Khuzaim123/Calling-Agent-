"""UrduSpeech parsing/sampling and script-aware scoring (pure functions, no downloads)."""
import json

from bench.datasets import parse_us_transcriptions, select_round_robin
from bench.scoring import latin_recall, split_scripts
from voice_engine.text import normalize_for_scoring

# Real row shape from benchmark/US-benchmark-CS/short/PODCAST/clean_transcription.jsonl (2026-11-08);
# note Duration_seconds is sometimes a string and sometimes a number.
CS_ROWS = "\n".join([
    json.dumps({"Speaker_id": "SPEAKER_000", "Audio_category": "PODCAST", "Audio_Clip": "SPEAKER_000_PODCAST_0044.WAV",
                "Duration_seconds": "9", "Transcription": "x",
                "ground_truth": "تو I also found کہ وہ جو ایک پاکستان کا جو image دنیا میں بن چکا ہے نا"},
               ensure_ascii=False),
    "not json",
    json.dumps({"Audio_Clip": "", "ground_truth": "no clip"}),
    json.dumps({"Audio_Clip": "B.WAV", "Duration_seconds": 6, "Transcription": "fallback text"}),
])


def test_parse_us_transcriptions():
    rows = parse_us_transcriptions(CS_ROWS)
    assert [r["clip"] for r in rows] == ["SPEAKER_000_PODCAST_0044.WAV", "B.WAV"]
    assert rows[0]["ref"].startswith("تو I also found") and rows[1]["ref"] == "fallback text"


def test_select_round_robin_balances_categories():
    by_cat = {"news/": [("n3", 0), ("n1", 0), ("n2", 0)], "vlog/": [("v1", 0)], "film/": [("f1", 0), ("f2", 0)]}
    picked = [a for a, _ in select_round_robin(by_cat, 5)]
    assert picked == ["f1", "n1", "v1", "f2", "n2"]
    assert len(select_round_robin(by_cat, 100)) == 6


def _wav(path, seconds=0.1):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


def test_valid_wav_detects_broken_copies(tmp_path):
    from bench.datasets import valid_wav
    good = _wav(tmp_path / "good.WAV")
    assert valid_wav(good)
    truncated = tmp_path / "trunc.WAV"
    truncated.write_bytes(good.read_bytes()[:200])            # header claims more data than present
    assert not valid_wav(truncated)
    (tmp_path / "html.WAV").write_bytes(b"<html>error</html>")
    assert not valid_wav(tmp_path / "html.WAV")
    assert not valid_wav(tmp_path / "missing.WAV")


def test_benchmark_skips_unreadable_audio(tmp_path):
    from bench.stt_bench import load_items_audio
    good = _wav(tmp_path / "a.wav")
    (tmp_path / "b.WAV").write_bytes(b"not audio at all")
    items = [{"id": "a", "path": str(good)}, {"id": "b", "path": str(tmp_path / "b.WAV")}]
    ok, audio, skipped = load_items_audio(items)
    assert [it["id"] for it in ok] == ["a"] and list(audio) == ["a"]
    assert skipped[0]["id"] == "b" and "Error" in skipped[0]["error"]


def test_split_scripts_and_latin_recall():
    ref = normalize_for_scoring("تو I also found کہ image دنیا میں", "ur")
    ur, la = split_scripts(ref)
    assert la == ["i", "also", "found", "image"] and ur == ["تو", "کہ", "دنیا", "میں"]
    hyp = normalize_for_scoring("تو آئی also found کہ امیج دنیا میں", "ur")   # STT wrote two English words in Urdu script
    assert latin_recall(ref, hyp) == (2, 4)
    assert latin_recall(normalize_for_scoring("سب اردو", "ur"), hyp) == (0, 0)
