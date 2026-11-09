"""Download model candidates from config/models.yaml into $DRIVE_ROOT/models (persistent).

Verifies each repo/file exists before downloading so a renamed upstream file
fails with a clear message instead of a half-populated directory.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from .env import DRIVE_ROOT, code_dir


def catalog() -> dict:
    return yaml.safe_load((code_dir() / "config" / "models.yaml").read_text(encoding="utf-8"))


def _token():
    return os.environ.get("HF_TOKEN") or None


def gguf_path(llm_id: str) -> Path:
    m = {x["id"]: x for x in catalog()["llm"]}[llm_id]
    return DRIVE_ROOT / "models" / "llm" / m["file"]


def download(kind: str, ids: list[str]) -> dict[str, str]:
    from huggingface_hub import hf_hub_download, list_repo_files, snapshot_download

    items = {x["id"]: x for x in catalog()[kind]}
    out = {}
    for mid in ids:
        if mid not in items:
            raise KeyError(f"{mid} not in config/models.yaml [{kind}]")
        m = items[mid]
        if "repo" not in m:
            out[mid] = "no download (API engine)"
            continue
        files = list_repo_files(m["repo"], token=_token())
        if kind == "llm":
            if m["file"] not in files:
                raise RuntimeError(f"{m['repo']} no longer contains {m['file']}")
            dest = DRIVE_ROOT / "models" / "llm"
            p = hf_hub_download(m["repo"], m["file"], local_dir=dest, token=_token())
            out[mid] = f"{p} ({Path(p).stat().st_size / 2**30:.2f} GiB)"
        elif mid == "kokoro":
            # kokoro's KPipeline fetches weights/voices itself into HF_HOME (on Drive)
            out[mid] = "fetched on first use into HF_HOME"
        else:
            dest = DRIVE_ROOT / "models" / m["dir"]
            snapshot_download(m["repo"], local_dir=dest, token=_token(),
                              allow_patterns=["*.json", "*.bin", "*.txt", "*.safetensors", "*.model", "vocabulary*"])
            size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
            out[mid] = f"{dest} ({size / 2**30:.2f} GiB)"
        print(f"[{kind}] {mid}: {out[mid]}  license: {m.get('license')}")
    return out
