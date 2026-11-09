"""Run project modules as child processes with the correct environment, streaming output."""
from __future__ import annotations

import subprocess
import sys

from .env import child_env, code_dir


def py(module: str, *args: str, check: bool = True, extra_env: dict | None = None) -> int:
    cmd = [sys.executable, "-m", module, *args]
    print("$", " ".join(cmd))
    p = subprocess.Popen(cmd, cwd=code_dir(), env=child_env(extra_env), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert p.stdout is not None
    for line in p.stdout:
        print(line, end="")
    rc = p.wait()
    if check and rc != 0:
        raise RuntimeError(f"{module} exited with code {rc}")
    return rc
