"""Quest ↔ Neon phone clock offset from syncPulse pairs (Phase 3)."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any


def quest_ms_to_phone_ns(quest_unix_ms: int, offset_quest_to_phone_ns: int) -> int:
    return int(quest_unix_ms) * 1_000_000 + int(offset_quest_to_phone_ns)


def pulse_offset_ns(quest_sent_unix_ms: int, neon_event_ns: int) -> int:
    return int(neon_event_ns) - int(quest_sent_unix_ms) * 1_000_000


def _valid_pulse(record: dict[str, Any]) -> bool:
    if not record.get("neon_event_ok"):
        return False
    quest_ms = record.get("quest_sent_unix_ms")
    neon_ns = record.get("neon_event_ns")
    return quest_ms is not None and neon_ns is not None


def group_pulse_batches(
    records: list[dict[str, Any]],
    *,
    gap_ms: int = 10_000,
) -> list[list[dict[str, Any]]]:
    """Split valid pulses into batches separated by a gap in Quest send time."""
    valid = [r for r in records if _valid_pulse(r)]
    if not valid:
        return []

    valid.sort(key=lambda r: int(r["quest_sent_unix_ms"]))
    batches: list[list[dict[str, Any]]] = [[valid[0]]]
    for rec in valid[1:]:
        prev_ms = int(batches[-1][-1]["quest_sent_unix_ms"])
        cur_ms = int(rec["quest_sent_unix_ms"])
        if cur_ms - prev_ms > gap_ms:
            batches.append([rec])
        else:
            batches[-1].append(rec)
    return batches


def compute_sync_from_pulses(
    records: list[dict[str, Any]],
    *,
    use_latest_batch: bool = True,
) -> dict[str, Any] | None:
    """Median offset from paired quest_sync pulses. Returns None if no valid pairs."""
    batches = group_pulse_batches(records)
    if not batches:
        return None

    batch = batches[-1] if use_latest_batch else batches[0]
    offsets = [
        pulse_offset_ns(int(r["quest_sent_unix_ms"]), int(r["neon_event_ns"]))
        for r in batch
    ]
    if not offsets:
        return None

    median = float(sorted(offsets)[len(offsets) // 2])
    if len(offsets) > 1:
        spread = float(math.sqrt(sum((o - median) ** 2 for o in offsets) / (len(offsets) - 1)))
    else:
        spread = 0.0

    pulse_rows = []
    for r in batch:
        quest_ms = int(r["quest_sent_unix_ms"])
        neon_ns = int(r["neon_event_ns"])
        pulse_rows.append({
            "seq": r.get("seq"),
            "quest_sent_unix_ms": quest_ms,
            "neon_event_ns": neon_ns,
            "pc_recv_unix_ns": r.get("pc_recv_unix_ns"),
            "offset_ns": pulse_offset_ns(quest_ms, neon_ns),
        })

    return {
        "version": 1,
        "offset_quest_to_phone_ns": int(round(median)),
        "offset_spread_std_ns": int(round(spread)),
        "pulse_count": len(batch),
        "formula": "neon_event_ns - quest_sent_unix_ms * 1e6",
        "usage": "t_utc_ns = quest_sent_unix_ms * 1e6 + offset_quest_to_phone_ns",
        "pulses": pulse_rows,
        "written_unix_ns": time.time_ns(),
    }


def load_sync_pulses_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def write_sync_json(path: str | Path, payload: dict[str, Any]) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return out
