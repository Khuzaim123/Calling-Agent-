"""HFP call audio via PipeWire (pulse compatibility tools `parec` / `pacat`).

With WirePlumber's `bluez5.hfphsp-backend = "ofono"`, PipeWire creates a
Bluetooth source (phone downlink = the remote party) and sink (uplink = what the
remote party hears) when the phone opens the SCO audio link for a call. We ask
PipeWire for 16 kHz mono PCM; it converts from CVSD (8 kHz) or mSBC (16 kHz).

Playout is paced by us (one frame per frame period, silence when idle) so that
a barge-in `clear` drops queued speech immediately instead of it sitting in a
large OS buffer.
STATUS: node naming and SCO behaviour NOT yet verified on the Redmi 9C.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time

log = logging.getLogger("gateway.audio")


def _hex(addr: str) -> str:
    return re.sub(r"[^0-9a-f]", "", addr.lower())


def pick_nodes(sources: str, sinks: str, bt_address: str) -> tuple[str | None, str | None]:
    """Choose the phone's Bluetooth source/sink from `pactl list short sources|sinks` output."""
    want = _hex(bt_address)

    def pick(listing: str) -> str | None:
        for line in listing.splitlines():
            cols = line.split("\t")
            name = cols[1] if len(cols) > 1 else ""
            if "bluez" not in name or name.endswith(".monitor"):
                continue
            if want and want not in _hex(name):
                continue
            return name
        return None
    return pick(sources), pick(sinks)


async def _run(*cmd: str) -> str:
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {err.decode(errors='replace')[:200]}")
    return out.decode(errors="replace")


async def find_nodes(bt_address: str, wait_s: float) -> tuple[str, str]:
    deadline = time.monotonic() + wait_s
    while True:
        src, snk = pick_nodes(await _run("pactl", "list", "short", "sources"),
                              await _run("pactl", "list", "short", "sinks"), bt_address)
        if src and snk:
            return src, snk
        if time.monotonic() > deadline:
            raise RuntimeError(f"no Bluetooth call audio nodes for {bt_address or 'any phone'} "
                               f"(source={src}, sink={snk}); is the call audio routed to the gateway?")
        await asyncio.sleep(0.25)


class PipeAudio:
    """Real AudioLink: capture = phone downlink, play = phone uplink."""

    def __init__(self, bt_address: str, rate: int, frame_ms: int, latency_ms: int, wait_s: float):
        for tool in ("pactl", "parec", "pacat"):
            if not shutil.which(tool):
                raise RuntimeError(f"{tool} not found (apt install pulseaudio-utils)")
        self.bt, self.rate, self.wait_s = bt_address, rate, wait_s
        self.frame_bytes = rate * frame_ms // 1000 * 2
        self.frame_s = frame_ms / 1000
        self.latency_ms = latency_ms
        self._queue = bytearray()
        self._rec = self._play = self._pacer = None
        self.nodes: tuple[str, str] | None = None

    async def open(self) -> None:
        src, snk = await find_nodes(self.bt, self.wait_s)
        self.nodes = (src, snk)
        fmt = ["--raw", "--format=s16le", f"--rate={self.rate}", "--channels=1", f"--latency-msec={self.latency_ms}"]
        self._rec = await asyncio.create_subprocess_exec("parec", f"--device={src}", *fmt,
                                                         stdout=asyncio.subprocess.PIPE)
        self._play = await asyncio.create_subprocess_exec("pacat", "--playback", f"--device={snk}", *fmt,
                                                          stdin=asyncio.subprocess.PIPE)
        self._pacer = asyncio.create_task(self._pace())
        log.info("audio open: downlink %s, uplink %s @ %d Hz", src, snk, self.rate)

    async def frames(self):
        assert self._rec and self._rec.stdout
        while True:
            try:
                yield await self._rec.stdout.readexactly(self.frame_bytes)
            except asyncio.IncompleteReadError:
                return

    def play(self, pcm: bytes) -> None:
        self._queue.extend(pcm)

    def clear(self) -> None:
        self._queue.clear()

    async def _pace(self) -> None:
        silence = bytes(self.frame_bytes)
        nxt = time.monotonic()
        while True:
            if len(self._queue) >= self.frame_bytes:
                frame = bytes(self._queue[: self.frame_bytes])
                del self._queue[: self.frame_bytes]
            else:
                frame = silence
            self._play.stdin.write(frame)
            await self._play.stdin.drain()
            nxt += self.frame_s
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))

    async def close(self) -> None:
        if self._pacer:
            self._pacer.cancel()
        for p in (self._rec, self._play):
            if p and p.returncode is None:
                p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), 3)
                except asyncio.TimeoutError:
                    p.kill()
