"""Quest ↔ PC clock offset via Neon-style Time Echo (NTP midpoint).

Neon / Pupil Labs formula (client=PC, host=Quest)::

    offset_ms = ((t1 + t2) / 2) - tH
    pc_time_ms = quest_time_ms + offset_ms

Refs:
  https://pupil-labs.github.io/pl-realtime-api/dev/methods/async/others/
  pupil_labs.realtime_api.time_echo
"""

from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path
from typing import Any


def quest_ms_to_pc_ns(quest_unix_ms: int, offset_quest_to_pc_ns: int) -> int:
    return int(quest_unix_ms) * 1_000_000 + int(offset_quest_to_pc_ns)


def phone_ns_to_pc_ns(phone_unix_ns: int, offset_phone_to_pc_ns: int) -> int:
    return int(phone_unix_ns) + int(offset_phone_to_pc_ns)


def echo_offset_ms(*, pc_t1_ms: int, pc_t2_ms: int, quest_tH_ms: int) -> float:
    """NTP-style midpoint offset: PC mid − Quest host time."""
    return ((int(pc_t1_ms) + int(pc_t2_ms)) / 2.0) - float(quest_tH_ms)


def echo_rtt_ms(*, pc_t1_ms: int, pc_t2_ms: int) -> int:
    return int(pc_t2_ms) - int(pc_t1_ms)


def summarize_echoes(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Build sync payload from time-echo samples (each has offset_ms, rtt_ms)."""
    if not samples:
        return None

    offsets_ms = [float(s["offset_ms"]) for s in samples]
    rtts_ms = [float(s["rtt_ms"]) for s in samples]
    median_ms = statistics.median(offsets_ms)
    mean_ms = statistics.mean(offsets_ms)
    spread_ms = statistics.stdev(offsets_ms) if len(offsets_ms) > 1 else 0.0
    rtt_mean = statistics.mean(rtts_ms)
    rtt_std = statistics.stdev(rtts_ms) if len(rtts_ms) > 1 else 0.0

    offset_ns = int(round(median_ms * 1_000_000))
    spread_ns = int(round(spread_ms * 1_000_000))

    return {
        "version": 2,
        "method": "time_echo",
        "offset_quest_to_pc_ns": offset_ns,
        "offset_quest_to_pc_ms_median": float(median_ms),
        "offset_quest_to_pc_ms_mean": float(mean_ms),
        "offset_spread_std_ns": spread_ns,
        "offset_spread_std_ms": float(spread_ms),
        "roundtrip_ms_mean": float(rtt_mean),
        "roundtrip_ms_std": float(rtt_std),
        "sample_count": len(samples),
        "formula": "offset_ms = ((pc_t1 + pc_t2) / 2) - quest_tH; pc_ms = quest_ms + offset_ms",
        "usage": "t_pc_ns = quest_unix_ms * 1e6 + offset_quest_to_pc_ns",
        "samples": [
            {
                "pc_t1_ms": s.get("pc_t1_ms"),
                "pc_t2_ms": s.get("pc_t2_ms"),
                "quest_tH_ms": s.get("quest_tH_ms"),
                "offset_ms": s.get("offset_ms"),
                "rtt_ms": s.get("rtt_ms"),
            }
            for s in samples
        ],
        "written_unix_ns": time.time_ns(),
    }


def merge_phone_offset(payload: dict[str, Any], offset_phone_to_pc_ns: int, **extra: Any) -> dict[str, Any]:
    out = dict(payload)
    out["offset_phone_to_pc_ns"] = int(offset_phone_to_pc_ns)
    out.update(extra)
    # Convenience: Quest→phone if both legs known (same PC hub).
    if "offset_quest_to_pc_ns" in out:
        out["offset_quest_to_phone_ns"] = int(out["offset_quest_to_pc_ns"]) - int(
            offset_phone_to_pc_ns
        )
    return out


def write_sync_json(path: str | Path, payload: dict[str, Any]) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return out


# --- legacy one-way pulse helpers (kept for offline recompute of old logs) ---

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
        "method": "sync_pulse_one_way",
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
