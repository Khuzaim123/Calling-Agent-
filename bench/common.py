"""Shared benchmark helpers: paths, GPU memory sampling, keyword scoring."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
import time
from pathlib import Path

import yaml

from voice_engine.text import normalize_for_scoring

ROOT = Path(os.environ.get("VOICE_AGENT_ROOT", Path(__file__).resolve().parent.parent))
CODE = Path(__file__).resolve().parent.parent


def engine_config_path() -> Path:
    """Runtime config written by the notebook (active models), else the repo default."""
    p = os.environ.get("VOICE_ENGINE_CONFIG")
    return Path(p) if p else CODE / "config" / "voice_engine.yaml"


def results_dir() -> Path:
    d = ROOT / "results" / "phase4"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_yaml(rel: str) -> dict:
    return yaml.safe_load((CODE / rel).read_text(encoding="utf-8"))


def write_json(name: str, data) -> Path:
    p = results_dir() / name
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def environment() -> dict:
    env = {"python": platform.python_version(), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=10).stdout.strip()
        env["gpu"] = out
    except (OSError, subprocess.TimeoutExpired):
        env["gpu"] = "unavailable"
    try:
        import torch
        env["torch"] = torch.__version__
        env["torch_cuda"] = torch.version.cuda
    except ImportError:
        pass
    return env


def gpu_used_mb() -> float | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        return float(out[0])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def gpu_processes_mb() -> dict[int, float]:
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        return {int(p): float(m) for p, m in (line.split(",") for line in out if line.strip())}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


class GpuPeak:
    """Poll total GPU memory in a background thread; `.peak_mb` after exit."""

    def __init__(self, interval_s: float = 0.1):
        self.interval = interval_s
        self.peak_mb: float | None = None
        self._stop = threading.Event()

    def _run(self):
        while not self._stop.is_set():
            v = gpu_used_mb()
            if v is not None and (self.peak_mb is None or v > self.peak_mb):
                self.peak_mb = v
            self._stop.wait(self.interval)

    def __enter__(self):
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()


def keyword_hits(hyp: str, keywords: list[list[str]], language: str) -> tuple[int, int]:
    """Count keyword groups found in the hypothesis (any alternative in a group counts)."""
    h = " " + normalize_for_scoring(hyp, language) + " "
    h_nospace = h.replace(" ", "")
    hits = 0
    for group in keywords:
        for alt in group:
            a = normalize_for_scoring(str(alt), language)
            if a and ((" " + a + " ") in h or (a.isdigit() and a in h_nospace)):
                hits += 1
                break
    return hits, len(keywords)
