"""Safety policy for a personal SIM: who may be called, who the AI may answer, and how much.

Enforced on the gateway (closest to the SIM) regardless of what n8n requests,
so a workflow bug cannot spend the balance or call arbitrary numbers.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# Pakistani numbers in E.164: mobile +92 3xx xxxxxxx; geographic +92 <area><subscriber>.
PK_MOBILE = re.compile(r"^\+923\d{9}$")
PK_FIXED = re.compile(r"^\+92[2-9]\d{8,9}$")
PREMIUM_PREFIXES = ("+92900",)               # 0900 premium-rate numbers are never dialled


def normalize_pk(number: str) -> str | None:
    """'0300-1234567', '923001234567', '+92 300 1234567' -> '+923001234567'; None if not a PK number."""
    n = re.sub(r"[\s\-()]", "", number or "")
    if n.startswith("00"):
        n = "+" + n[2:]
    elif n.startswith("0"):
        n = "+92" + n[1:]
    elif n.startswith("92"):
        n = "+" + n
    if n.startswith(PREMIUM_PREFIXES):
        return None
    return n if (PK_MOBILE.match(n) or PK_FIXED.match(n)) else None


@dataclass
class PolicyCfg:
    outbound_mode: str = "allowlist"          # off | allowlist | pakistan
    outbound_allowlist: list[str] = field(default_factory=list)
    inbound_mode: str = "allowlist"           # off | allowlist | all
    inbound_allowlist: list[str] = field(default_factory=list)
    answer_after_s: float = 6.0               # ring this long first, so you can answer personally
    max_call_seconds: int = 300
    max_outbound_per_hour: int = 6
    max_outbound_minutes_per_day: int = 30
    dial_timeout_s: int = 45                  # give up if not answered


class Policy:
    def __init__(self, cfg: PolicyCfg, state_file: Path):
        self.cfg = cfg
        self.state_file = state_file
        self.out_allow = {normalize_pk(n) for n in cfg.outbound_allowlist} - {None}
        self.in_allow = {normalize_pk(n) for n in cfg.inbound_allowlist} - {None}
        self.state = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"dials": [], "day": "", "day_seconds": 0}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        tmp.replace(self.state_file)

    def _roll_day(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.state.get("day") != day:
            self.state.update(day=day, day_seconds=0)

    def check_outbound(self, number: str, now: float | None = None) -> tuple[str | None, str]:
        """Return (normalized_number, "") if allowed, else (None, reason)."""
        now = time.time() if now is None else now
        n = normalize_pk(number)
        if self.cfg.outbound_mode == "off":
            return None, "outbound_disabled"
        if n is None:
            return None, "not_a_pakistani_number"
        if self.cfg.outbound_mode == "allowlist" and n not in self.out_allow:
            return None, "number_not_in_allowlist"
        recent = [t for t in self.state.get("dials", []) if now - t < 3600]
        if len(recent) >= self.cfg.max_outbound_per_hour:
            return None, "hourly_dial_limit"
        self._roll_day(now)
        if self.state["day_seconds"] >= self.cfg.max_outbound_minutes_per_day * 60:
            return None, "daily_minutes_limit"
        return n, ""

    def record_dial(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.state["dials"] = [t for t in self.state.get("dials", []) if now - t < 3600] + [now]
        self._save()

    def record_outbound_seconds(self, seconds: float, now: float | None = None) -> None:
        self._roll_day(time.time() if now is None else now)
        self.state["day_seconds"] += int(seconds)
        self._save()

    def should_answer(self, number: str) -> tuple[bool, str]:
        if self.cfg.inbound_mode == "off":
            return False, "inbound_disabled"
        if self.cfg.inbound_mode == "all":
            return True, ""
        n = normalize_pk(number)
        if n and n in self.in_allow:
            return True, ""
        return False, "caller_not_in_allowlist"
