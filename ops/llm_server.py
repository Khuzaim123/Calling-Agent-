"""llama.cpp llama-server: get the binary, serve one GGUF model.

How the binary is obtained (first that works wins):
  1. already present in this VM (/content/runtime/llama);
  2. a binary compiled in an earlier session and cached on Drive;
  3. llama.cpp's official prebuilt Ubuntu CUDA 12.8 release (~165 MB; the ~567 MB CUDA runtime
     package only if Colab's own libraries are missing). Downloaded ONCE into the Drive cache and
     unpacked from there in later sessions. Verified with --list-devices;
  4. compile from source for sm_75 (slow on Colab's 2 CPUs: 30-60 min). Only when the downloaded
     binary cannot use the GPU, or forced with LLAMA_FORCE_BUILD=1. Download problems (e.g. GitHub 504)
     are retried and then reported; they never trigger a compile.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

from .env import DRIVE_ROOT, RUNTIME, child_env
from .services import Service, http_ok

LLAMA_TAG = os.environ.get("LLAMA_CPP_TAG", "b11700")   # 2026-10-16
PREBUILT_CUDA = os.environ.get("LLAMA_PREBUILT_CUDA", "12.8")
REPO = "https://github.com/ggml-org/llama.cpp"
RELEASES = f"{REPO}/releases/download"
PORT = 8081
MARKER = RUNTIME / "llama" / "prebuilt.json"


def cuda_version() -> str:
    r = subprocess.run(["nvcc", "--version"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("nvcc not found; a GPU runtime with the CUDA toolkit is required")
    line = [x for x in r.stdout.splitlines() if "release" in x][0]
    return line.split("release")[1].split(",")[0].strip()


def _compiled_binary() -> Path:
    return RUNTIME / "llama" / "bin" / "llama-server"


def _marker() -> dict:
    try:
        return json.loads(MARKER.read_text())
    except (OSError, ValueError):
        return {}


def binary() -> Path:
    m = _marker()
    return Path(m["binary"]) if m.get("binary") else _compiled_binary()


def lib_dirs() -> list[str]:
    return _marker().get("lib_dirs", [])


def run_env() -> dict[str, str]:
    """Environment for running the binary: prebuilt CUDA/ggml libraries first on the library path."""
    env = child_env()
    if lib_dirs():
        env["LD_LIBRARY_PATH"] = os.pathsep.join(lib_dirs() + [env.get("LD_LIBRARY_PATH", "")]).rstrip(os.pathsep)
    return env


def find_layout(root: Path) -> tuple[Path | None, list[str]]:
    """Locate llama-server and every directory holding shared libraries inside an extracted release."""
    exe = next((p for p in sorted(root.rglob("llama-server")) if p.is_file()), None)
    libs = sorted({str(p.parent) for p in root.rglob("*.so*") if p.is_file() or p.is_symlink()})
    return exe, libs


def _extract(archive: Path, dest: Path) -> None:
    with tarfile.open(archive) as tf:
        try:
            tf.extractall(dest, filter="data")      # refuses paths outside dest (Python >= 3.12)
        except TypeError:
            tf.extractall(dest)


class DownloadError(RuntimeError):
    """Network problem fetching the prebuilt release (retryable; never a reason to compile)."""


def _download(name: str, dest: Path, attempts: int = 6) -> Path:
    out = dest / name
    if out.exists() and out.stat().st_size > 0:
        return out
    url = f"{RELEASES}/{LLAMA_TAG}/{name}"
    tmp = out.with_suffix(".partial")
    print("downloading", url)
    if shutil.which("curl"):
        # --retry-all-errors also retries HTTP 5xx such as GitHub's transient 504; -C - resumes partial files
        r = subprocess.run(["curl", "-fL", "--retry", str(attempts), "--retry-delay", "5", "--retry-all-errors",
                            "--connect-timeout", "30", "-C", "-", "-sS", "-o", str(tmp), url])
        if r.returncode != 0:
            raise DownloadError(f"curl exit {r.returncode} after {attempts} retries")
    else:
        last = None
        for i in range(attempts):
            try:
                urllib.request.urlretrieve(url, tmp)
                break
            except OSError as e:
                last = e
                print(f"  attempt {i + 1}/{attempts} failed: {e}")
                time.sleep(5 * (i + 1))
        else:
            raise DownloadError(str(last))
    tmp.rename(out)
    return out


def _fetch_extract(name: str, dl: Path, base: Path) -> None:
    """Download + extract one release archive once; a sentinel marks completed extraction."""
    done = base / f".{name}.ok"
    if done.exists():
        return
    try:
        archive = _download(name, dl)
    except DownloadError as e:
        raise DownloadError(f"Could not download {name} from GitHub ({e}). This is a network problem, "
                            f"not a reason to compile: wait a minute and re-run this cell "
                            f"(partial downloads resume).") from None
    try:
        _extract(archive, base)
    except (tarfile.TarError, EOFError, OSError) as e:
        archive.unlink(missing_ok=True)                  # corrupt download: start clean next time
        raise DownloadError(f"{name} was incomplete or corrupt ({e}); re-run this cell to download it again") from None
    done.touch()                                      # archive is kept in the Drive cache for the next VM


def _check(base: Path) -> tuple[bool, str]:
    """Record the layout and ask the binary to list GPUs. Returns (sees_cuda, output)."""
    exe, libs = find_layout(base)
    if exe is None:
        return False, "prebuilt archive has no llama-server binary"
    exe.chmod(exe.stat().st_mode | 0o111)
    MARKER.write_text(json.dumps({"binary": str(exe), "lib_dirs": libs, "tag": LLAMA_TAG, "cuda": PREBUILT_CUDA}))
    r = subprocess.run([str(exe), "--list-devices"], capture_output=True, text=True, env=run_env(), timeout=60)
    out = r.stdout + r.stderr
    return r.returncode == 0 and "CUDA" in out, out


def _try_prebuilt() -> Path | None:
    base = RUNTIME / "llama" / "prebuilt"
    dl = DRIVE_ROOT / "cache" / "llama.cpp"      # archives stay on Drive: later sessions never hit GitHub
    base.mkdir(parents=True, exist_ok=True)
    dl.mkdir(parents=True, exist_ok=True)
    main = f"llama-{LLAMA_TAG}-bin-ubuntu-cuda-{PREBUILT_CUDA}-x64.tar.gz"
    cudart = f"cudart-llama-{LLAMA_TAG}-bin-ubuntu-cuda-{PREBUILT_CUDA}-x64.tar.gz"
    _fetch_extract(main, dl, base)                       # ~165 MB: the program itself
    ok, out = _check(base)                               # first try Colab's own CUDA libraries
    if not ok and ("shared librar" in out or "cannot open shared object" in out):
        print("CUDA runtime libraries missing; downloading NVIDIA runtime package (~567 MB) ...")
        _fetch_extract(cudart, dl, base)
        ok, out = _check(base)
    if not ok:
        print("prebuilt llama-server cannot use the GPU here:\n" + out[-800:])
        MARKER.unlink(missing_ok=True)
        return None
    print("prebuilt llama-server ready:", binary())
    print([line.strip() for line in out.splitlines() if "CUDA" in line][:3])
    return binary()


def _compile() -> Path:
    cache = DRIVE_ROOT / "cache" / "llama.cpp" / f"llama-server-{LLAMA_TAG}-cuda{cuda_version()}-sm75.tar"
    tags = subprocess.run(["git", "ls-remote", "--tags", REPO, f"refs/tags/{LLAMA_TAG}"],
                          capture_output=True, text=True, check=True).stdout
    if LLAMA_TAG not in tags:
        raise RuntimeError(f"llama.cpp tag {LLAMA_TAG} not found")
    src = RUNTIME / "tmp" / "llama.cpp"
    shutil.rmtree(src, ignore_errors=True)
    subprocess.run(["git", "clone", "--depth", "1", "--branch", LLAMA_TAG, REPO, str(src)], check=True)
    print("Compiling llama-server with CUDA for sm_75 (fallback; 30-60 min on Colab's 2 CPUs) ...")
    flags = ["-DGGML_CUDA=ON", "-DCMAKE_CUDA_ARCHITECTURES=75", "-DBUILD_SHARED_LIBS=OFF",
             "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_USE_PREBUILT_UI=OFF",
             "-DCMAKE_BUILD_TYPE=Release"]
    subprocess.run(["cmake", "-S", str(src), "-B", str(src / "build")] + flags, check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run(["cmake", "--build", str(src / "build"), "--target", "llama-server", "-j", str(os.cpu_count() or 2)],
                   check=True, stdout=subprocess.DEVNULL)
    built = src / "build" / "bin" / "llama-server"
    if not built.exists():
        raise RuntimeError("build finished but llama-server binary is missing")
    _compiled_binary().parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, _compiled_binary())
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(cache, "w") as tf:
        tf.add(_compiled_binary(), arcname="bin/llama-server")
    shutil.rmtree(src, ignore_errors=True)
    print("llama-server compiled and cached at", cache)
    return _compiled_binary()


def ensure_built() -> Path:
    if binary().exists():
        return binary()
    cache = DRIVE_ROOT / "cache" / "llama.cpp" / f"llama-server-{LLAMA_TAG}-cuda{cuda_version()}-sm75.tar"
    if cache.exists():                                   # compiled in an earlier session
        MARKER.unlink(missing_ok=True)
        (RUNTIME / "llama").mkdir(parents=True, exist_ok=True)
        _extract(cache, RUNTIME / "llama")
        print("llama-server restored from Drive cache", cache.name)
        return _compiled_binary()
    if os.environ.get("LLAMA_FORCE_BUILD") != "1":
        exe = _try_prebuilt()
        if exe is not None:
            return exe
        print("falling back to compiling from source")
    return _compile()


def version() -> str:
    r = subprocess.run([str(binary()), "--version"], capture_output=True, text=True, env=run_env(), timeout=60)
    return (r.stdout + r.stderr).strip()


def service(model_path: Path, ctx: int = 8192, parallel: int = 2, template_kwargs: str | None = None) -> Service:
    if not model_path.exists():
        raise FileNotFoundError(model_path)
    cmd = [str(binary()), "-m", str(model_path), "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "all", "-c", str(ctx), "-np", str(parallel), "--jinja", "--metrics"]
    if template_kwargs:
        cmd += ["--chat-template-kwargs", template_kwargs]
    key = os.environ.get("LLM_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    env = {"LLAMA_API_KEY": key}            # API key via env so it never appears in `ps` output
    if lib_dirs():
        env["LD_LIBRARY_PATH"] = run_env()["LD_LIBRARY_PATH"]
    return Service("llm", cmd, env=env, health=lambda: http_ok(f"http://127.0.0.1:{PORT}/health", headers))
