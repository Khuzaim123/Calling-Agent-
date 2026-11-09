"""Locating llama-server and its libraries in the extracted prebuilt release (layout seen 2026-11-08:
llama-b11700/{llama-server, libggml-cuda.so, ...} and cudart-llama-.../libcublas.so.12)."""
from ops.llm_server import find_layout


def test_find_layout(tmp_path):
    main = tmp_path / "llama-b11700"
    cudart = tmp_path / "cudart-llama-b11700-bin-ubuntu-cuda-12.8-x64"
    main.mkdir()
    cudart.mkdir()
    for f in ("llama-server", "llama-cli", "libggml-cuda.so", "libllama.so"):
        (main / f).write_bytes(b"x")
    for f in ("libcublas.so.12", "libcudart.so.12"):
        (cudart / f).write_bytes(b"x")
    exe, libs = find_layout(tmp_path)
    assert exe == main / "llama-server"
    assert set(libs) == {str(main), str(cudart)}


def test_find_layout_missing_binary(tmp_path):
    (tmp_path / "x.so").write_bytes(b"x")
    assert find_layout(tmp_path)[0] is None
