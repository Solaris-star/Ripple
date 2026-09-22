"""Cost-bearing campaign work: three durable, bounded daily admission windows.

This ledger is separate from provider snapshots. Claim before network/model work;
unknown outcomes are not replayed after a crash. Free polling uses its own clock.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock

TIMEZONE = "Asia/Shanghai"
DAILY_TIMES = ("09:00", "14:00", "20:00")
WINDOW_SECONDS = 60


def daily_slot(now: float) -> tuple[str, int] | None:
    local = datetime.fromtimestamp(now, ZoneInfo(TIMEZONE))
    for clock in DAILY_TIMES:
        hour, minute = map(int, clock.split(":"))
        at = int(local.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp())
        if 0 <= now - at < WINDOW_SECONDS:
            return f"{local.date().isoformat()}T{clock}", at
    return None


def next_daily_at(now: float) -> int:
    local = datetime.fromtimestamp(now, ZoneInfo(TIMEZONE))
    for day in (local, local + timedelta(days=1)):
        for clock in DAILY_TIMES:
            hour, minute = map(int, clock.split(":"))
            at = int(day.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp())
            if at > now:
                return at
    raise AssertionError("next daily slot must exist")


class CampaignPaidSchedule:
    def __init__(self, path: Path):
        self.path = path
        self.lock = FileLock(str(path) + ".lock", timeout=5)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "claims": {}}
        # A malformed ledger is a safety failure, never an empty budget.
        if self.path.stat().st_size > 64 * 1024:
            raise ValueError("Campaign paid schedule ledger exceeds bounds")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("claims"), dict):
            raise ValueError("Invalid campaign paid schedule ledger")
        for key, rows in data["claims"].items():
            if not isinstance(key, str) or not isinstance(rows, dict) or any(
                not isinstance(slot, str) or type(at) is not int or at < 0
                for slot, at in rows.items()
            ):
                raise ValueError("Invalid campaign paid schedule claim")
        return data

    def available(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        slot = daily_slot(now)
        if slot is None:
            return False
        try:
            data = self._read()
        except (OSError, ValueError):
            return False
        return slot[0] not in data["claims"].get(key, {})

    def next_at(self, key: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        slot = daily_slot(now)
        return slot[1] if slot and self.available(key, now) else next_daily_at(now)

    def claim(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        slot = daily_slot(now)
        if slot is None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            data = self._read()
            claims = data["claims"].setdefault(key, {})
            if slot[0] in claims:
                return False
            claims[slot[0]] = int(now)
            # Retain more than the current day to prevent clock/restart replay.
            for name, rows in data["claims"].items():
                data["claims"][name] = dict(sorted(rows.items(), reverse=True)[:21])
            temp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                os.replace(temp, self.path)
            finally:
                temp.unlink(missing_ok=True)
            return True
