"""Benchmark datasets.

FLEURS (google/fleurs, CC-BY-4.0): read speech, en_us and ur_pk test splits.
  Layout verified 2026-11-07: data/<lang>/test.tsv (7 tab-separated columns:
  id, file_name, raw_transcription, transcription, chars, num_samples, gender)
  and data/<lang>/audio/test.tar.gz with members test/<file_name>.
UrduSpeech (ASLP-lab/UrduSpeech, CC-BY-4.0; Haq et al. 2026, arXiv:2605.17846): only the
  manually corrected benchmark split. Layout verified 2026-11-08:
  benchmark/US-benchmark-{CS,Std,EngPk}/{short,long}/<CATEGORY>/clean_transcription.jsonl
  (fields Audio_Clip, ground_truth, ...) and .../<CATEGORY>/audio/<Audio_Clip> (44.1 kHz mono WAV).
  CS = Urdu-English code-switched, Std = Pakistani Urdu, EngPk = Pakistani-accented English.
Own recordings (optional, no longer used by the notebook): WAVs named
  <prompt_id>__<speaker>__<take>.wav from bench/prompts.yaml `stt_read`.

  python -m bench.datasets fleurs --lang ur_pk --n 60
  python -m bench.datasets urduspeech --subset CS --n 60
  python -m bench.datasets own
"""
from __future__ import annotations

import argparse
import csv
import json
import tarfile
from pathlib import Path

from .common import ROOT, load_yaml

LANG = {"ur_pk": "ur", "en_us": "en"}


def data_dir() -> Path:
    d = ROOT / "datasets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_fleurs(lang: str, n: int) -> Path:
    from huggingface_hub import hf_hub_download

    if lang not in LANG:
        raise ValueError(f"lang must be one of {list(LANG)}")
    cache = data_dir() / "hf_cache"
    tsv = hf_hub_download("google/fleurs", f"data/{lang}/test.tsv", repo_type="dataset", cache_dir=cache)
    rows = list(csv.reader(open(tsv, encoding="utf-8"), delimiter="\t", quoting=csv.QUOTE_NONE))
    if not rows or len(rows[0]) != 7:
        raise RuntimeError(f"FLEURS {lang} test.tsv format changed: expected 7 columns, got {len(rows[0]) if rows else 0}")
    wanted = {}
    for r in rows:
        if len(wanted) >= n:
            break
        wanted.setdefault(r[1], r)
    out = data_dir() / "fleurs" / lang
    out.mkdir(parents=True, exist_ok=True)
    missing = [f for f in wanted if not (out / f).exists()]
    if missing:
        tgz = hf_hub_download("google/fleurs", f"data/{lang}/audio/test.tar.gz", repo_type="dataset", cache_dir=cache)
        need = set(missing)
        with tarfile.open(tgz, "r:gz") as tf:
            for m in tf:
                name = Path(m.name).name
                if m.isfile() and name in need:
                    src = tf.extractfile(m)
                    (out / name).write_bytes(src.read())
                    need.discard(name)
                    if not need:
                        break
        if need:
            raise RuntimeError(f"{len(need)} FLEURS files listed in test.tsv were not found in the archive")
    manifest = data_dir() / f"manifest_fleurs_{lang}.jsonl"
    with manifest.open("w", encoding="utf-8") as fh:
        for fname, r in wanted.items():
            fh.write(json.dumps({"id": f"fleurs_{lang}_{Path(fname).stem}", "path": str(out / fname),
                                 "language": LANG[lang], "group": f"fleurs_{LANG[lang]}",
                                 "refs": [r[3]], "keywords": []}, ensure_ascii=False) + "\n")
    print(f"FLEURS {lang}: {len(wanted)} utterances -> {manifest}")
    return manifest


US_REPO = "ASLP-lab/UrduSpeech"
US_SUBSETS = {"CS": ("ur", "us_mixed"), "Std": ("ur", "us_urdu"), "EngPk": ("en", "us_english_pk")}


def parse_us_transcriptions(text: str) -> list[dict]:
    """Rows with a clip name and a non-empty reference; malformed lines are skipped."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ref = str(r.get("ground_truth") or r.get("Transcription") or "").strip()
        clip = str(r.get("Audio_Clip") or "").strip()
        if clip and ref:
            rows.append({"clip": clip, "ref": ref, "category": r.get("Audio_category", ""),
                         "speaker": r.get("Speaker_id", "")})
    return rows


def select_round_robin(by_category: dict[str, list], n: int) -> list:
    """Deterministic, category-balanced sample: one item per category in turn until n."""
    queues = [sorted(items, key=lambda x: str(x[0])) for _, items in sorted(by_category.items())]
    out, i = [], 0
    while len(out) < n and any(queues):
        q = queues[i % len(queues)]
        if q:
            out.append(q.pop(0))
        i += 1
    return out


def fetch_urduspeech(subset: str, n: int, length: str = "short") -> Path:
    from huggingface_hub import hf_hub_download, list_repo_files

    if subset not in US_SUBSETS:
        raise ValueError(f"subset must be one of {list(US_SUBSETS)}")
    language, group = US_SUBSETS[subset]
    # local_dir => real files. The default HF cache uses symlinks, which Google Drive does not
    # support reliably (a corrupted WAV on Drive was observed on 2026-11-08).
    local = data_dir() / "urduspeech"
    files = set(list_repo_files(US_REPO, repo_type="dataset"))
    prefix = f"benchmark/US-benchmark-{subset}/{length}/"
    jsonls = sorted(f for f in files if f.startswith(prefix) and f.endswith("/clean_transcription.jsonl"))
    if not jsonls:
        raise RuntimeError(f"UrduSpeech layout changed: no {prefix}*/clean_transcription.jsonl")
    by_cat: dict[str, list] = {}
    for j in jsonls:
        cat_dir = j[: -len("clean_transcription.jsonl")]
        text = Path(hf_hub_download(US_REPO, j, repo_type="dataset", local_dir=local)).read_text(encoding="utf-8")
        for r in parse_us_transcriptions(text):
            audio = f"{cat_dir}audio/{r['clip']}"
            if audio in files:
                by_cat.setdefault(cat_dir, []).append((audio, r))
    ordered = select_round_robin(by_cat, sum(len(v) for v in by_cat.values()))
    manifest = data_dir() / f"manifest_urduspeech_{subset}.jsonl"
    written, skipped = 0, []
    with manifest.open("w", encoding="utf-8") as fh:
        for audio, r in ordered:
            if written >= n:
                break
            path = Path(hf_hub_download(US_REPO, audio, repo_type="dataset", local_dir=local))
            if not valid_wav(path):                   # broken copy: fetch again once, then give up on it
                path = Path(hf_hub_download(US_REPO, audio, repo_type="dataset", local_dir=local,
                                            force_download=True))
            if not valid_wav(path):
                skipped.append(audio)
                continue
            fh.write(json.dumps({"id": f"us_{subset}_{Path(r['clip']).stem}", "path": str(path),
                                 "language": language, "group": group, "refs": [r["ref"]], "keywords": [],
                                 "category": r["category"]}, ensure_ascii=False) + "\n")
            written += 1
    print(f"UrduSpeech {subset}/{length}: {written} utterances from {len(by_cat)} categories -> {manifest}")
    if skipped:
        print(f"  skipped {len(skipped)} unreadable files: {skipped[:3]}")
    return manifest


def valid_wav(path: Path) -> bool:
    """RIFF/WAVE header present and the file is not shorter than its header says (truncated copy)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
        size = path.stat().st_size
    except OSError:
        return False
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return False
    return size >= int.from_bytes(head[4:8], "little") + 8


def build_own_manifest() -> Path:
    prompts = {p["id"]: p for p in load_yaml("bench/prompts.yaml")["stt_read"]}
    src = data_dir() / "own"
    src.mkdir(parents=True, exist_ok=True)
    manifest = data_dir() / "manifest_own.jsonl"
    n = 0
    with manifest.open("w", encoding="utf-8") as fh:
        for wav in sorted(src.glob("*.wav")):
            pid = wav.stem.split("__")[0]
            p = prompts.get(pid)
            if p is None:
                print(f"skip {wav.name}: unknown prompt id {pid}")
                continue
            group = "own_mixed" if pid.startswith("mx") else f"own_{p['language']}"
            fh.write(json.dumps({"id": wav.stem, "path": str(wav), "language": p["language"], "group": group,
                                 "refs": p["refs"], "keywords": p["keywords"]}, ensure_ascii=False) + "\n")
            n += 1
    print(f"own recordings: {n} files -> {manifest}")
    return manifest


def load_manifest(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fleurs")
    f.add_argument("--lang", required=True, choices=list(LANG))
    f.add_argument("--n", type=int, default=60)
    u = sub.add_parser("urduspeech")
    u.add_argument("--subset", required=True, choices=list(US_SUBSETS))
    u.add_argument("--n", type=int, default=60)
    u.add_argument("--length", default="short", choices=["short", "long"])
    sub.add_parser("own")
    a = ap.parse_args()
    if a.cmd == "fleurs":
        fetch_fleurs(a.lang, a.n)
    elif a.cmd == "urduspeech":
        fetch_urduspeech(a.subset, a.n, a.length)
    else:
        build_own_manifest()


if __name__ == "__main__":
    main()
