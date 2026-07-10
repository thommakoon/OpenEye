from __future__ import annotations
import json
import socket
import threading
import time
from typing import Optional, Tuple

HOST, PORT = "0.0.0.0", 5051

class TcpServer(threading.Thread):
    def __init__(self, status_cb, message_cb=None):
        super().__init__(daemon=True)
        self.status_cb = status_cb
        self.message_cb = message_cb  # called (thread) with parsed dict from Quest
        self.server: Optional[socket.socket] = None
        self.conn: Optional[socket.socket] = None
        self.addr: Optional[Tuple[str, int]] = None
        self._stop = False

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
            if self.message_cb is not None:
                try:
                    self.message_cb(msg)
                except Exception as e:
                    print(f"[TCP] message_cb error: {e}")

    def _send(self, msg: dict):
        if not self.conn:
            return
        try:
            payload = json.dumps(msg).encode("utf-8")
            header = len(payload).to_bytes(4, byteorder="big")
            self.conn.sendall(header + payload)
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
        self._send({
            "type": "gazeVisual",
            "payload": payload,
        })
        print(payload.get("t_ns", ts), x, y)

    def close(self):
        self._stop = True
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
