"""Periodic Quest↔PC (and optional Neon↔PC) time-echo monitor."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from .networking import TcpServer
from .sync_offset import merge_phone_offset, summarize_echoes, write_sync_json


class QuestTimeEchoMonitor(threading.Thread):
    """Runs Neon-style round-trips on a fixed PC period.

    Initial burst (default 100) locks sync.json, then one sample each period
    updates a rolling window and rewrites sync.json.
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
        neon_estimate_fn: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
        neon_every_n_periods: int = 10,
        status_cb: Optional[Callable[[str], None]] = None,
        sync_written_cb: Optional[Callable[[dict[str, Any]], None]] = None,
    ):
        super().__init__(daemon=True)
        self.tcp = tcp
        self.participant_dir_fn = participant_dir_fn
        self.period_s = float(period_s)
        self.burst_n = int(burst_n)
        self.rolling_n = int(rolling_n)
        self.echo_timeout_s = float(echo_timeout_s)
        self.neon_estimate_fn = neon_estimate_fn
        self.neon_every_n_periods = max(1, int(neon_every_n_periods))
        self.status_cb = status_cb
        self.sync_written_cb = sync_written_cb
        self._stop = threading.Event()
        self._samples: list[dict[str, Any]] = []
        self._phone_offset_ns: Optional[int] = None
        self._phone_meta: dict[str, Any] = {}
        self._lock = threading.Lock()

    def stop(self):
        self._stop.set()

    def _status(self, msg: str):
        if self.status_cb:
            try:
                self.status_cb(msg)
            except Exception:
                pass
        print(f"[timeEcho] {msg}")

    def _collect(self, n: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for _ in range(n):
            if self._stop.is_set():
                break
            sample = self.tcp.request_time_echo(timeout_s=self.echo_timeout_s)
            if sample is None:
                continue
            sample["pc_unix_ns"] = time.time_ns()
            out.append(sample)
        return out

    def _maybe_neon(self):
        if self.neon_estimate_fn is None:
            return
        try:
            est = self.neon_estimate_fn()
        except Exception as e:
            self._status(f"neon estimate failed: {e}")
            return
        if not est:
            return
        with self._lock:
            self._phone_offset_ns = int(est["offset_phone_to_pc_ns"])
            self._phone_meta = {k: v for k, v in est.items() if k != "offset_phone_to_pc_ns"}

    def _write_sync(self, samples: list[dict[str, Any]]):
        payload = summarize_echoes(samples)
        if payload is None:
            return
        with self._lock:
            phone_ns = self._phone_offset_ns
            phone_meta = dict(self._phone_meta)
        if phone_ns is not None:
            payload = merge_phone_offset(payload, phone_ns, **phone_meta)

        pdir = self.participant_dir_fn()
        if not pdir:
            self._status("no participant dir; sync.json not written")
            return
        path = f"{pdir}/sync.json"
        write_sync_json(path, payload)
        self._status(
            f"sync.json offset_quest_to_pc_ms="
            f"{payload['offset_quest_to_pc_ms_median']:.2f}±"
            f"{payload['offset_spread_std_ms']:.2f} "
            f"rtt={payload['roundtrip_ms_mean']:.1f}ms n={payload['sample_count']}"
        )
        if self.sync_written_cb:
            try:
                self.sync_written_cb(payload)
            except Exception:
                pass

    def run(self):
        self._status(f"burst×{self.burst_n} then every {self.period_s:.1f}s")
        burst = self._collect(self.burst_n)
        if len(burst) < 3:
            self._status(f"burst failed ({len(burst)} ok); stopping")
            return
        # Drop first sample (Warm-up), mirror Pupil TimeOffsetEstimator.
        if len(burst) > 1:
            burst = burst[1:]
        with self._lock:
            self._samples = list(burst)[-self.rolling_n :]
            samples_snap = list(self._samples)
        self._maybe_neon()
        self._write_sync(samples_snap)

        period_i = 0
        while not self._stop.wait(self.period_s):
            period_i += 1
            one = self._collect(1)
            if one:
                with self._lock:
                    self._samples.extend(one)
                    self._samples = self._samples[-self.rolling_n :]
                    samples_snap = list(self._samples)
                self._write_sync(samples_snap)
            else:
                self._status("echo timeout")
            if period_i % self.neon_every_n_periods == 0:
                self._maybe_neon()
                with self._lock:
                    samples_snap = list(self._samples)
                if samples_snap:
                    self._write_sync(samples_snap)

        self._status("stopped")
