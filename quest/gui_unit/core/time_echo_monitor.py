"""Periodic Quest↔PC and Neon↔PC time-echo monitor (PC hub clock)."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from .networking import TcpServer
from .sync_offset import (
    merge_phone_offset,
    summarize_echoes,
    summarize_phone_echoes,
    write_sync_json,
)


class PcHubSyncMonitor(threading.Thread):
    """Runs Neon-style round-trips on a fixed PC period for Quest and Neon.

    Initial Quest burst (default 100) locks sync.json, then one Quest + one Neon
    sample each period updates rolling windows and rewrites sync.json.
    """

    def __init__(
        self,
        tcp: TcpServer,
        *,
        participant_dir_fn: Callable[[], Optional[str]],
        period_s: float = 1.0,
        burst_n: int = 100,
        rolling_n: int = 100,
        echo_timeout_s: float = 0.5,
        neon_sample_fn: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
        neon_burst_fn: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
        quest_echo_cb: Optional[Callable[[dict[str, Any]], None]] = None,
        neon_echo_cb: Optional[Callable[[dict[str, Any]], None]] = None,
        status_cb: Optional[Callable[[str], None]] = None,
        sync_written_cb: Optional[Callable[[dict[str, Any]], None]] = None,
        require_quest: bool = True,
    ):
        super().__init__(daemon=True)
        self.tcp = tcp
        self.participant_dir_fn = participant_dir_fn
        self.period_s = float(period_s)
        self.burst_n = int(burst_n)
        self.rolling_n = int(rolling_n)
        self.echo_timeout_s = float(echo_timeout_s)
        self.neon_sample_fn = neon_sample_fn
        self.neon_burst_fn = neon_burst_fn or neon_sample_fn
        self.quest_echo_cb = quest_echo_cb
        self.neon_echo_cb = neon_echo_cb
        self.status_cb = status_cb
        self.sync_written_cb = sync_written_cb
        self.require_quest = bool(require_quest)
        # Must not be named `_stop` — that shadows threading.Thread._stop and
        # breaks is_alive() after the worker exits.
        self._halt = threading.Event()
        self._quest_samples: list[dict[str, Any]] = []
        self._neon_samples: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._last_quest_timeout_log = 0.0
        self._quest_timeout_streak = 0

    def stop(self):
        self._halt.set()

    def _status(self, msg: str):
        if self.status_cb:
            try:
                self.status_cb(msg)
            except Exception:
                pass
        print(f"[pcHubSync] {msg}")

    def _collect_quest(self, n: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        timeouts = 0
        for i in range(n):
            if self._halt.is_set():
                break
            sample = self.tcp.request_time_echo(timeout_s=self.echo_timeout_s)
            if sample is None:
                timeouts += 1
                if timeouts == 1 or timeouts == 5 or (timeouts % 25 == 0):
                    self._status(
                        f"quest echo timeout ({timeouts}/{i + 1}) — "
                        "is Practice/Main/OpenEye calib (with timeEcho) TCP-connected?"
                    )
                continue
            sample["pc_unix_ns"] = time.time_ns()
            out.append(sample)
            if self.quest_echo_cb:
                try:
                    self.quest_echo_cb(sample)
                except Exception:
                    pass
        return out

    def _collect_neon(self, *, burst: bool = False) -> Optional[dict[str, Any]]:
        fn = self.neon_burst_fn if burst else self.neon_sample_fn
        if fn is None:
            return None
        try:
            sample = fn()
        except Exception as e:
            self._status(f"neon echo failed: {e}")
            return None
        if not sample:
            return None
        sample = dict(sample)
        sample.setdefault("pc_unix_ns", time.time_ns())
        if self.neon_echo_cb:
            try:
                self.neon_echo_cb(sample)
            except Exception:
                pass
        return sample

    def _append_neon(self, sample: dict[str, Any]):
        with self._lock:
            self._neon_samples.append(sample)
            self._neon_samples = self._neon_samples[-self.rolling_n :]

    def _write_sync(self):
        with self._lock:
            quest_snap = list(self._quest_samples)
            neon_snap = list(self._neon_samples)

        payload = summarize_echoes(quest_snap)
        if payload is None and self.require_quest:
            return

        phone_stats = summarize_phone_echoes(neon_snap)
        if payload is None:
            # Neon-only fallback (no Quest TCP yet).
            if phone_stats is None:
                return
            payload = {
                "version": 2,
                "method": "time_echo_neon_only",
                "written_unix_ns": time.time_ns(),
            }
        if phone_stats is not None:
            payload = merge_phone_offset(payload, phone_stats)

        pdir = self.participant_dir_fn()
        if not pdir:
            self._status("no participant dir; sync.json not written")
            return
        path = f"{pdir}/sync.json"
        write_sync_json(path, payload)

        quest_ms = payload.get("offset_quest_to_pc_ms_median")
        phone_ms = payload.get("phone_offset_ms_median")
        parts = []
        if quest_ms is not None:
            parts.append(
                f"quest={quest_ms:.1f}±{payload.get('offset_spread_std_ms', 0):.1f}ms"
            )
        if phone_ms is not None:
            parts.append(
                f"neon={phone_ms:.1f}±{payload.get('phone_offset_spread_std_ms', 0):.1f}ms"
            )
        self._status("sync.json " + " ".join(parts))

        if self.sync_written_cb:
            try:
                self.sync_written_cb(payload)
            except Exception:
                pass

    def run(self):
        self._status(
            f"Quest burst×{self.burst_n}, then Quest+Neon every {self.period_s:.1f}s"
        )

        neon_boot = self._collect_neon(burst=True)
        if neon_boot:
            self._append_neon(neon_boot)

        burst = self._collect_quest(self.burst_n)
        if len(burst) < 3:
            if self.require_quest:
                self._status(
                    f"quest burst failed ({len(burst)} ok); sync.json not started. "
                    "Connect Quest app with timeEcho support."
                )
                if not self._neon_samples:
                    return
            else:
                self._status(f"quest burst skipped/failed ({len(burst)} ok)")
        else:
            if len(burst) > 1:
                burst = burst[1:]
            with self._lock:
                self._quest_samples = list(burst)[-self.rolling_n :]

        self._write_sync()

        while not self._halt.wait(self.period_s):
            neon_one = self._collect_neon(burst=False)
            if neon_one:
                self._append_neon(neon_one)

            quest_one = self._collect_quest(1)
            if quest_one:
                self._quest_timeout_streak = 0
                with self._lock:
                    self._quest_samples.extend(quest_one)
                    self._quest_samples = self._quest_samples[-self.rolling_n :]
            elif self.require_quest:
                self._quest_timeout_streak += 1
                now = time.time()
                # Rate-limit: first miss, then every 10s while streak continues.
                if (
                    self._quest_timeout_streak == 1
                    or (now - self._last_quest_timeout_log) >= 10.0
                ):
                    self._last_quest_timeout_log = now
                    self._status(
                        f"quest echo timeout (streak={self._quest_timeout_streak}) — "
                        "TCP Connected to Practice/Main with timeEcho?"
                    )

            self._write_sync()

        self._status("stopped")


# Back-compat alias
QuestTimeEchoMonitor = PcHubSyncMonitor
