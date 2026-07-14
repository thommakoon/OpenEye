from __future__ import annotations
import json
import socket
import threading
import time
from typing import Any, Optional, Tuple

HOST, PORT = "0.0.0.0", 5051


def _time_ms() -> int:
    return time.time_ns() // 1_000_000


class TcpServer(threading.Thread):
    def __init__(self, status_cb, message_cb=None):
        super().__init__(daemon=True)
        self.status_cb = status_cb
        self.message_cb = message_cb  # called (thread) with parsed dict from Quest
        self.server: Optional[socket.socket] = None
        self.conn: Optional[socket.socket] = None
        self.addr: Optional[Tuple[str, int]] = None
        self._stop = False
        self._send_lock = threading.Lock()
        # Neon-style time-echo request/response (see request_time_echo).
        self._echo_lock = threading.Lock()
        self._echo_event = threading.Event()
        self._echo_expect_t1: Optional[int] = None
        self._echo_result: Optional[dict[str, Any]] = None

    def run(self):
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, PORT))
            self.server.listen(1)
        except Exception as e:
            self.status_cb(f"TCP error: {e}")
            return

        # Accept loop: keep serving new clients (e.g. after OpenEye hands off
        # to PracticeTask, PracticeTask connects as a fresh client).
        while not self._stop:
            self.status_cb(f"Waiting...{HOST}:{PORT}")
            try:
                conn, addr = self.server.accept()
            except OSError:
                break  # server socket closed
            if self._stop:
                try: conn.close()
                except Exception: pass
                break
            self.conn, self.addr = conn, addr
            try:
                self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self.status_cb(f"Connected: {self.addr}")
            try:
                self._recv_loop()
            except Exception as e:
                self.status_cb(f"recv error: {e}")
            finally:
                try:
                    if self.conn:
                        self.conn.close()
                except Exception:
                    pass
                self.conn = None

    def _recv_exact(self, n: int) -> Optional[bytes]:
        buf = b""
        while len(buf) < n:
            chunk = self.conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _recv_loop(self):
        while True:
            header = self._recv_exact(4)
            if header is None:
                self.status_cb("Quest disconnected")
                return
            length = int.from_bytes(header, byteorder="big")
            if length <= 0 or length > 10_000_000:
                self.status_cb(f"bad length: {length}")
                return
            payload = self._recv_exact(length)
            if payload is None:
                self.status_cb("Quest disconnected")
                return
            try:
                msg = json.loads(payload.decode("utf-8"))
            except Exception as e:
                print(f"[TCP] parse error: {e}")
                continue

            # Handle timeEcho replies locally (request_time_echo waiter).
            if isinstance(msg, dict) and msg.get("type") == "timeEcho":
                t2 = _time_ms()
                pl = msg.get("payload") or {}
                t1 = pl.get("pc_t1_ms")
                tH = pl.get("quest_tH_ms")
                with self._echo_lock:
                    if (
                        self._echo_expect_t1 is not None
                        and t1 is not None
                        and int(t1) == int(self._echo_expect_t1)
                        and tH is not None
                    ):
                        self._echo_result = {
                            "pc_t1_ms": int(t1),
                            "pc_t2_ms": int(t2),
                            "quest_tH_ms": int(tH),
                            "rtt_ms": int(t2) - int(t1),
                            "offset_ms": ((int(t1) + int(t2)) / 2.0) - float(tH),
                        }
                        self._echo_event.set()
                continue

            if self.message_cb is not None:
                try:
                    self.message_cb(msg)
                except Exception as e:
                    print(f"[TCP] message_cb error: {e}")

    def _send(self, msg: dict, *, quiet: bool = False):
        if not self.conn:
            return
        try:
            payload = json.dumps(msg).encode("utf-8")
            header = len(payload).to_bytes(4, byteorder="big")
            with self._send_lock:
                self.conn.sendall(header + payload)
            if not quiet:
                print(f"sent: {msg}")
        except Exception as e:
            self.status_cb(f"send message error: {e}")

    def send_step(self, step_value: int):
        self._send({"type": "updateStep", "payload": {"step": int(step_value)}})

    def send_end_signal(self):
        self._send({"type": "calibrationEnd", "payload": {}})

    def send_reset_calib(self):
        self._send({"type": "resetCalib", "payload": {}})

    def send_launch_app(self, package: str = ""):
        self._send({"type": "launchApp", "payload": {"package": str(package)}})

    def send_eval_target(self, idx: int, t_ms: int, pos: tuple[float, float]):
        self._send({
            "type": "evalTarget",
            "payload": {
                "idx": int(idx),
                "t_ms": int(t_ms),
                "pos": {"x": float(pos[0]), "y": float(pos[1])},
            },
        })
    
    def send_gaze_visual(self, ts: float, x: float, y: float, ts_ns: int | None = None):
        payload = {"t": float(ts), "x": float(x), "y": float(y)}
        if ts_ns is not None:
            payload["t_ns"] = int(ts_ns)
        # High-rate path: skip console I/O (was a major cost at 30–100 Hz).
        self._send({"type": "gazeVisual", "payload": payload}, quiet=True)

    def request_time_echo(self, timeout_s: float = 1.0) -> Optional[dict[str, Any]]:
        """Neon-style round-trip: PC t1 → Quest replies (t1, tH) → PC t2.

        Returns dict with pc_t1_ms, pc_t2_ms, quest_tH_ms, rtt_ms, offset_ms
        where offset_ms = ((t1+t2)/2) - tH  (pc_ms = quest_ms + offset_ms).
        """
        if not self.conn:
            return None
        with self._echo_lock:
            self._echo_event.clear()
            self._echo_result = None
            t1 = _time_ms()
            self._echo_expect_t1 = t1
        self._send(
            {"type": "timeEcho", "payload": {"pc_t1_ms": int(t1), "quest_tH_ms": 0}},
            quiet=True,
        )
        if not self._echo_event.wait(timeout_s):
            with self._echo_lock:
                self._echo_expect_t1 = None
            return None
        with self._echo_lock:
            result = self._echo_result
            self._echo_expect_t1 = None
            self._echo_result = None
        return result

    def close(self):
        self._stop = True
        try:
            self._echo_event.set()
        except Exception:
            pass
        try:
            if self.conn:
                self.conn.close()
        finally:
            if self.server:
                self.server.close()

class EvalPlanStreamer(threading.Thread):
    def __init__(self, tcp_thread: TcpServer, plan_path: str, tick_hz: int=20):
        super().__init__(daemon=True)
        self.tcp = tcp_thread
        self.plan_path = plan_path
        self.tick_hz = int(tick_hz)
        self._running = threading.Event(); self._running.set()

    def stop(self):
        self._running.clear()

    def run(self):
        try:
            with open(self.plan_path, "r", encoding="utf-8") as f:
                import json
                plan = json.load(f)
        except Exception as e:
            print(f"[EVAL] load error: {e}")
            return

        timeline = plan.get("timeline", [])
        if not timeline:
            print("[EVAL] empty timeline")
            return

        dt = 1.0 / float(self.tick_hz)
        next_time = time.perf_counter()

        for idx, item in enumerate(timeline):
            if not self._running.is_set():
                break
            t_ms = int(item.get("t_ms", idx * (1000 // self.tick_hz)))
            pos = item.get("pos", {})
            pos_xy = (float(pos.get("x", 0.0)), float(pos.get("y", 0.0)))

            self.tcp.send_eval_target(idx, t_ms, pos_xy)

            next_time += dt
            sleep_dur = next_time - time.perf_counter()
            if sleep_dur > 0:
                time.sleep(sleep_dur)
