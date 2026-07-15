import argparse
import sys
import os
import threading
import time
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, List, Dict

import pandas as pd
import numpy as np
# ---- PySide6 GUI ----
from PySide6.QtCore import Qt, QTimer, QPointF, Signal
from PySide6.QtGui import QPixmap, QPainter, QPen, QColor, QFont, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QCheckBox, QSpinBox, QComboBox, QGroupBox, QMessageBox
)
# ---- Pupil Labs Real-Time API ----
from pupil_labs.realtime_api.simple import discover_one_device

# ==============================
# Local
# ==============================
from ..core.config import DEFAULT_CONFIG as CFG
from ..core.networking import TcpServer, EvalPlanStreamer
from ..core.filter import ButterLPFilter
from ..core.logger import JsonLogger
from ..core.sync_offset import compute_sync_from_pulses, write_sync_json
from ..core.time_echo_monitor import QuestTimeEchoMonitor
from ..core.mapping import (
    normalize_neon_xy,
    map_biquadratic, map_ridge_biquadratic,
    predict_biquad, predict_ridge_biquad,
    save_models, load_models,
)

# =========================================================
# Configuration
# =========================================================

CANVAS_W = CFG.canvas.width_px
CANVAS_H = CFG.canvas.height_px

EVAL_RATE_HZ = CFG.eval.rate_hz
EVAL_DURATION_S = CFG.eval.duration_s
EVAL_DWELL_MS = CFG.eval.dwell_ms
H_RANGE_DEG = CFG.eval.h_range_deg
V_RANGE_DEG = CFG.eval.v_range_deg

def generate_calib_targets(
    h_range_deg=H_RANGE_DEG,
    v_range_deg=V_RANGE_DEG,
    view_dist_m=CFG.eval.view_distance_m,
    grid_size=(5, 5),
):
    cols, rows = grid_size
    h_deg = np.linspace(h_range_deg[0], h_range_deg[1], cols)
    v_deg = np.linspace(v_range_deg[1], v_range_deg[0], rows)

    h_rad = np.radians(h_deg)
    v_rad = np.radians(v_deg)

    xs = np.tan(h_rad) * view_dist_m
    ys = np.tan(v_rad) * view_dist_m

    coords = [(float(x), float(y)) for y in ys for x in xs]
    return coords

CALIB_TARGET_COORDINATES = generate_calib_targets()

# ==============================
# Data / threads
# ==============================

class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.ended = False
        self.recording = False
        self.step = 0

        self.latest_gaze_filtered = (None, None)
        self.gaze_log = []
        self.event: Optional[str] = None
        self.datum_lines = []

        self.eval_active = False
        self.eval_logger = None
        self.tracking_active = False
        self.tracking_logger = None
        self.models = {}
    
    def update_latest_filtered_gaze(self, x, y):
        with self.lock:
            self.latest_gaze_filtered = (x, y)
    
    def append_gaze_log(self, ts, filt_x, filt_y, raw_x, raw_y, event: str=""):
        with self.lock:
            self.gaze_log.append((float(ts), float(filt_x), float(filt_y), float(raw_x), float(raw_y), event))
    
    def set_event(self):
        with self.lock:
            self.event = "event_log"
    
    def consume_event(self):
        with self.lock:
            e = self.event or ""
            self.event = None
            return e

    def snapshot_gaze_log(self):
        with self.lock:
            return list(self.gaze_log)
        
    def add_datum(self, datum):
        line = json.dumps(datum, ensure_ascii=False)
        with self.lock:
            self.datum_lines.append(line)
    
    # ---- evaluation logging ----
    def start_evaluation(self, log_path: str, models: dict, canvas_w: int, canvas_h: int):
        self.models = models or {}
        self.canvas_w, self.canvas_h = int(canvas_w), int(canvas_h)
        self.eval_logger = JsonLogger(log_path, flush_every=100, flush_ms=100)
        self.eval_logger.start()
        self.eval_active = True

    def stop_evaluation(self):
        self.eval_active = False
        if self.eval_logger:
            self.eval_logger.stop()
            self.eval_logger.join(timeout=1.0)
            self.eval_logger = None
    
    def append_eval_record(self, ts: float, ts_ns: int, x_f: float, y_f: float, x_raw: float, y_raw: float):
        if not (self.eval_active and self.eval_logger):
            return
        neon_xy_f = np.array([[float(x_f), float(y_f)]], dtype=float)
        neon_xy_n = normalize_neon_xy(neon_xy_f, self.canvas_w, self.canvas_h)
        msg = {
            "type": "gazeEval",
            "payload": {
                "t": float(ts),
                "t_ns": int(ts_ns),
                "raw": {"x": float(x_raw), "y": float(y_raw)},
                "filtered": {"x": float(x_f), "y": float(y_f)},
                "mapped": self._predict_all(neon_xy_n)
            }
        }
        self.eval_logger.log(msg)
    
    # ---- gaze tracking logging ----
    def start_tracking(self, log_path: str, models: dict, canvas_w: int, canvas_h: int):
        self.models = models or {}
        self.canvas_w, self.canvas_h = int(canvas_w), int(canvas_h)
        self.tracking_logger = JsonLogger(log_path, flush_every=100, flush_ms=100)
        self.tracking_logger.start()
        self.tracking_active = True
    
    def stop_tracking(self):
        self.tracking_active = False
        if self.tracking_logger:
            self.tracking_logger.stop()
            self.tracking_logger.join(timeout=1.0)
            self.tracking_logger = None

    def append_tracking_record(self, ts: float, ts_ns: int, x_f: float, y_f: float, x_raw: float, y_raw: float):
        if not (self.tracking_active and self.tracking_logger):
            return
        neon_xy_f = np.array([[float(x_f), float(y_f)]], dtype=float)
        neon_xy_n = normalize_neon_xy(neon_xy_f, self.canvas_w, self.canvas_h)
        msg = {
            "type": "gazeTrack",
            "payload": {
                "t": float(ts),
                "t_ns": int(ts_ns),
                "raw": {"x": float(x_raw), "y": float(y_raw)},
                "filtered": {"x": float(x_f), "y": float(y_f)},
                "mapped": self._predict_all(neon_xy_n)
            }
        }
        self.tracking_logger.log(msg)

    def _predict_all(self, neon_xy_n: np.ndarray) -> dict:
        out = {}
        if "biquadratic" in self.models:
            xy = predict_biquad(self.models["biquadratic"], neon_xy_n)[0]
            out["biquadratic"] = {"x": float(xy[0]), "y": float(xy[1])}
        if "ridge_biquadratic" in self.models:
            xy = predict_ridge_biquad(self.models["ridge_biquadratic"], neon_xy_n)[0]
            out["ridge_biquadratic"] = {"x": float(xy[0]), "y": float(xy[1])}
        return out

class GazeTxThread(threading.Thread):
    """Latest-wins TCP sender off the Qt GUI thread."""

    def __init__(self, main_window: "MainWindow", rate_hz: float = 100.0):
        super().__init__(daemon=True)
        self.mw = main_window
        self.rate_hz = float(rate_hz)
        self._running = threading.Event()
        self._running.set()

    def stop(self):
        self._running.clear()

    def run(self):
        interval = 1.0 / max(1.0, self.rate_hz)
        next_t = time.perf_counter()
        while self._running.is_set():
            try:
                self.mw._send_latest_gaze_visual()
            except Exception:
                pass
            next_t += interval
            sleep_dur = next_t - time.perf_counter()
            if sleep_dur > 0:
                time.sleep(sleep_dur)
            else:
                # Fell behind: resync so we keep sending latest, not a backlog.
                next_t = time.perf_counter()

class GazeCollector(threading.Thread):
    def __init__(self, device, state: SharedState, filter: ButterLPFilter):
        super().__init__(daemon=True)
        self.device = device
        self.state = state
        self.filter = filter
        self.on_new_filtered = None

    def run(self):
        while self.state.running:
            datum = self.device.receive_gaze_datum()
            x, y = datum.x, datum.y
            ts = datum.timestamp_unix_seconds
            # Integer ns for matching Neon gaze.csv (Realtime API only exposes float seconds).
            ts_ns = int(getattr(datum, "timestamp_unix_ns", int(ts * 1e9)))

            x_f, y_f = self.filter.step(x, y)
            self.state.update_latest_filtered_gaze(x_f, y_f)
            if self.on_new_filtered is not None:
                try: self.on_new_filtered(ts, ts_ns, x_f, y_f, x, y)
                except Exception: pass
            self.state.append_eval_record(ts, ts_ns, x_f, y_f, x, y)
            self.state.append_tracking_record(ts, ts_ns, x_f, y_f, x, y)
            if self.state.recording:
                self.state.add_datum(datum)
                event = self.state.consume_event()
                self.state.append_gaze_log(ts, x_f, y_f, x, y, event)

# ==============================
# GUI widgets
# ==============================

class GazeCanvas(QLabel):
    def __init__(self):
        super().__init__()
        self.setFixedSize(400, 300)
        self.pix = QPixmap(CANVAS_W, CANVAS_H)
        self.pix.fill(Qt.black)
        self.setScaledContents(True)
        self.setPixmap(self.pix)

    def draw_frame(self, gaze_xy):
        x, y = gaze_xy if gaze_xy else (None, None)
        self.pix.fill(Qt.black)
        painter = QPainter(self.pix)
        painter.setRenderHint(QPainter.Antialiasing, True)

        painter.setPen(QPen(QColor(60, 60, 60), 4))
        painter.drawRect(2, 2, CANVAS_W-4, CANVAS_H-4)
        painter.setPen(QPen(QColor(200, 200, 200), 1))
        painter.setFont(QFont("Arial", 28))
        painter.drawText(20, 50, f"{CANVAS_W} x {CANVAS_H} Gaze Canvas")

        if x is not None and y is not None:
            px = int(max(0, min(CANVAS_W - 1, x)))
            py = int(max(0, min(CANVAS_H - 1, y)))
            painter.setPen(QPen(QColor(0, 200, 255), 3))
            painter.setBrush(QColor(0, 200, 255))
            painter.drawEllipse(QPointF(px, py), 8, 8)
            painter.setPen(QPen(QColor(200, 200, 200), 2, Qt.DashLine))
            painter.drawLine(px, 0, px, CANVAS_H)
            painter.drawLine(0, py, CANVAS_W, py)
            painter.setPen(QPen(QColor(200, 200, 200), 2))
            painter.setFont(QFont("Arial", 28))
            painter.drawText(20, 90, f"x={px}, y={py}")
        
        painter.end()
        self.setPixmap(self.pix)

# ==============================
# Main window
# ==============================

class MainWindow(QMainWindow):
    _tcp_status_changed = Signal(str)
    _remote_next_step = Signal()
    _sync_pulse_record = Signal(dict)
    _sync_status_changed = Signal(str)
    _main_study_done = Signal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Quest-Neon Calibration")
        self.state = SharedState()
        self.device = None
        self.gaze_thread: Optional[GazeCollector] = None
        self.tcp_thread: Optional[TcpServer] = None
        self.plan_streamer: Optional[EvalPlanStreamer] = None
        self.models: Dict[str, Dict] = {}
        self.eval_active = False
        self.tracking_active = False
        
        self.participant_dir = None
        self.calib_dir = None
        self.eval_log_path = None
        self.eval_log_dir = None

        self.gv_rate_hz = 100
        self._gv_lock = threading.Lock()
        self._gv_latest_raw = None
        self._gaze_tx_thread: Optional[GazeTxThread] = None
        self._sync_pulse_logger: Optional[JsonLogger] = None
        self.sync_pulses_path = None
        self.sync_json_path = None
        self._sync_pulse_batch: List[dict] = []
        self._time_echo_monitor: Optional[QuestTimeEchoMonitor] = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        ctrl_box = QGroupBox("Controls"); ctrl_layout = QHBoxLayout(ctrl_box)

        self.p_spin = QSpinBox(); self.p_spin.setRange(0, 99); self.p_spin.setValue(0)
        self.neon_status = QLabel("Neon: disconnected")
        self.tcp_status = QLabel("TCP: idle")
        self.step_label = QLabel("Step: idle")
        self.btn_connect_neon = QPushButton("Connect Neon")
        self.btn_start_tcp = QPushButton("Start TCP Server")
        ctrl_layout.addWidget(QLabel("trial:")); ctrl_layout.addWidget(self.p_spin); ctrl_layout.addStretch()
        ctrl_layout.addWidget(self.btn_connect_neon); ctrl_layout.addWidget(self.neon_status)
        ctrl_layout.addWidget(self.btn_start_tcp); ctrl_layout.addWidget(self.tcp_status)
        ctrl_layout.addWidget(self.step_label)

        calib_box = QGroupBox("Calibration"); calib_layout = QHBoxLayout(calib_box)
        self.btn_start_calib = QPushButton("Start Calibration (S)")
        self.btn_next_step = QPushButton("Next Step (Space)")
        self.btn_reset_calib = QPushButton("Reset Calibration")
        self.btn_next_step.setEnabled(False)
        self.calib_hint = QLabel("1. Connect Neon  2. Start TCP  3. Launch Quest  4. Start Calibration  5. Fixate each dot, Next Step ×25")
        calib_layout.addWidget(self.btn_start_calib)
        calib_layout.addWidget(self.btn_next_step)
        calib_layout.addWidget(self.btn_reset_calib)
        calib_layout.addWidget(self.calib_hint, stretch=1)

        self.canvas = GazeCanvas()
        task_box = QGroupBox("Tasks"); task_layout = QHBoxLayout(task_box)

        self.btn_eval = QPushButton("Start Evaluation")
        self.btn_track = QPushButton("Start Gaze Tracking")
        self.visualize = QCheckBox("Visualize")
        task_layout.addWidget(self.btn_eval); task_layout.addWidget(self.btn_track); task_layout.addWidget(self.visualize)

        study_box = QGroupBox("Study Handoff"); study_layout = QHBoxLayout(study_box)
        self.btn_launch_practice = QPushButton("Start Practice")
        self.btn_launch_study = QPushButton("Start Main Study")
        self.btn_recalibrate = QPushButton("Recalibrate (OpenEye)")
        self.study_hint = QLabel(
            "Practice = auto Fitts. Main Study = PC-commanded. Recalibrate → OpenEye calib."
        )
        study_layout.addWidget(self.btn_launch_practice)
        study_layout.addWidget(self.btn_launch_study)
        study_layout.addWidget(self.btn_recalibrate)
        study_layout.addWidget(self.study_hint, stretch=1)

        main_box = QGroupBox("Main Study commander (MRstress IDLE)")
        main_layout = QHBoxLayout(main_box)
        self.ms_sub = QSpinBox(); self.ms_sub.setRange(0, 999); self.ms_sub.setValue(0)
        self.ms_subsub = QSpinBox(); self.ms_subsub.setRange(0, 9); self.ms_subsub.setValue(0)
        self.ms_condition = QComboBox()
        self.ms_condition.addItems(["EyeDwell", "EyePinch", "HeadDwell", "HeadPinch"])
        self.ms_reps = QSpinBox(); self.ms_reps.setRange(1, 10); self.ms_reps.setValue(3)
        self.ms_duration_min = QSpinBox(); self.ms_duration_min.setRange(1, 30); self.ms_duration_min.setValue(5)
        self.ms_duration_min.setSuffix(" min")
        self.btn_ms_start = QPushButton("Start condition")
        self.ms_status = QLabel("Status: idle (Quest MainStudy must be connected)")
        main_layout.addWidget(QLabel("sub")); main_layout.addWidget(self.ms_sub)
        main_layout.addWidget(QLabel("subsub")); main_layout.addWidget(self.ms_subsub)
        main_layout.addWidget(QLabel("condition")); main_layout.addWidget(self.ms_condition)
        main_layout.addWidget(QLabel("reps")); main_layout.addWidget(self.ms_reps)
        main_layout.addWidget(QLabel("each")); main_layout.addWidget(self.ms_duration_min)
        main_layout.addWidget(self.btn_ms_start)
        main_layout.addWidget(self.ms_status, stretch=1)

        sync_box = QGroupBox("Clock sync (PC hub — Neon-style time-echo)")
        sync_layout = QHBoxLayout(sync_box)
        self.btn_time_echo = QPushButton("Start Quest↔PC time-echo")
        self.sync_period_spin = QSpinBox()
        self.sync_period_spin.setRange(1, 60)
        self.sync_period_spin.setValue(1)
        self.sync_period_spin.setSuffix(" s")
        self.sync_status = QLabel("Sync: idle")
        sync_layout.addWidget(self.btn_time_echo)
        sync_layout.addWidget(QLabel("period"))
        sync_layout.addWidget(self.sync_period_spin)
        sync_layout.addWidget(self.sync_status, stretch=1)

        root.addWidget(ctrl_box); root.addWidget(calib_box); root.addWidget(self.canvas)
        root.addWidget(task_box); root.addWidget(study_box); root.addWidget(main_box); root.addWidget(sync_box)

        # wiring
        self.btn_connect_neon.clicked.connect(self.on_connect_neon)
        self.btn_start_tcp.clicked.connect(self.on_start_tcp)
        self.btn_start_calib.clicked.connect(self.on_start_recording)
        self.btn_next_step.clicked.connect(self.trigger_step)
        self.btn_reset_calib.clicked.connect(self.on_reset_calibration)
        self._tcp_status_changed.connect(self._apply_tcp_status)
        self._remote_next_step.connect(self.trigger_step)
        self._sync_pulse_record.connect(self._on_sync_pulse_record)
        self._sync_status_changed.connect(self._apply_sync_status)
        self._main_study_done.connect(self._on_main_study_done)
        self.btn_time_echo.clicked.connect(self.on_toggle_time_echo)
        self.btn_eval.clicked.connect(self.on_toggle_evaluation)
        self.btn_track.clicked.connect(self.on_toggle_gaze_tracking)
        self.btn_launch_practice.clicked.connect(self.on_launch_practice)
        self.btn_launch_study.clicked.connect(self.on_launch_study)
        self.btn_ms_start.clicked.connect(self.on_main_study_start)
        self.btn_recalibrate.clicked.connect(self.on_recalibrate)
        self._ms_running = False

        # UI update timer
        self.timer = QTimer(self); self.timer.timeout.connect(self.repaint_canvas); self.timer.start(5)

        # shortcut
        self.setFocusPolicy(Qt.StrongFocus)

        self.shortcut_space = QShortcut(QKeySequence(Qt.Key_Space), self)
        self.shortcut_space.setContext(Qt.ApplicationShortcut)
        self.shortcut_space.activated.connect(self.trigger_step)

        self.shortcut_s = QShortcut(QKeySequence(Qt.Key_S), self)
        self.shortcut_s.setContext(Qt.ApplicationShortcut)
        self.shortcut_s.activated.connect(self.on_start_recording)

        self.shortcut_e = QShortcut(QKeySequence(Qt.Key_E), self)
        self.shortcut_e.setContext(Qt.ApplicationShortcut)
        self.shortcut_e.activated.connect(self.on_toggle_evaluation)

        self.shortcut_q = QShortcut(QKeySequence(Qt.Key_Q), self)
        self.shortcut_q.setContext(Qt.ApplicationShortcut)
        self.shortcut_q.activated.connect(self.cleanup_and_close)

    # ---- Helpers ----
    def _apply_tcp_status(self, msg: str):
        self.tcp_status.setText(f"TCP: {msg}")

    def _apply_sync_status(self, msg: str):
        self.sync_status.setText(f"Sync: {msg}")

    def status_tcp(self, msg):
        self._tcp_status_changed.emit(msg)

    def status_sync(self, msg: str):
        self._sync_status_changed.emit(msg)

    def repaint_canvas(self):
        with self.state.lock:
            gx_f, gy_f = self.state.latest_gaze_filtered
        self.canvas.draw_frame((gx_f, gy_f) if gx_f is not None else None)

    def ensure_dirs(self):
        p_num = self.p_spin.value()
        participant_id = f"t{p_num:02d}"
        self.participant_dir = participant_id
        self.calib_dir = os.path.join(self.participant_dir, "calibration")
        os.makedirs(self.calib_dir, exist_ok=True)

    def _ensure_sync_pulse_logger(self):
        self.ensure_dirs()
        if self._sync_pulse_logger is None:
            self.sync_pulses_path = os.path.join(self.participant_dir, "sync_pulses.jsonl")
            self._sync_pulse_logger = JsonLogger(self.sync_pulses_path)
            self._sync_pulse_logger.start()
    
    def stream_gaze_visual(self, ts: float, ts_ns: int, x_f: float, y_f: float, x_raw: float = 0.0, y_raw: float = 0.0):
        with self._gv_lock:
            self._gv_latest_raw = (float(ts), int(ts_ns), float(x_f), float(y_f))

    def _send_latest_gaze_visual(self):
        if not self.tracking_active:
            return
        chk = getattr(self, "visualize", None)
        if not (chk and chk.isChecked()):
            return
        if not (self.tcp_thread and self.tcp_thread.conn):
            return
        if "ridge_biquadratic" not in self.models:
            return

        with self._gv_lock:
            latest = self._gv_latest_raw
        if latest is None:
            return

        ts, ts_ns, x_f, y_f = latest

        neon_xy_f = np.array([[float(x_f), float(y_f)]], dtype=float)
        neon_xy_n = normalize_neon_xy(neon_xy_f, CANVAS_W, CANVAS_H)
        xy = predict_ridge_biquad(self.models["ridge_biquadratic"], neon_xy_n)[0]
        self.tcp_thread.send_gaze_visual(ts, float(xy[0]), float(xy[1]), ts_ns=ts_ns)

    def _start_gaze_tx(self):
        self._stop_gaze_tx()
        self._gaze_tx_thread = GazeTxThread(self, rate_hz=self.gv_rate_hz)
        self._gaze_tx_thread.start()

    def _stop_gaze_tx(self):
        if self._gaze_tx_thread is not None:
            self._gaze_tx_thread.stop()
            self._gaze_tx_thread.join(timeout=1.0)
            self._gaze_tx_thread = None


    # ---- Neon ----
    def on_connect_neon(self):
        try:
            self.ensure_dirs()
            self.neon_status.setText("Neon: ")
            device = discover_one_device(max_search_duration_seconds=10)
            if device is None:
                self.neon_status.setText("Neon: No device found")
                QMessageBox.warning(self, "Neon", "No device found.")
                return
            self.device = device
            self.neon_status.setText("Neon: Connected")

            self.state.running = True
            filter = ButterLPFilter(fs=CFG.filt.fs_hz, fc=CFG.filt.fc_hz, order=CFG.filt.order)
            self.gaze_thread = GazeCollector(self.device, self.state, filter=filter)
            self.gaze_thread.on_new_filtered = self.stream_gaze_visual
            self.gaze_thread.start()

        except Exception as e:
            self.neon_status.setText(f"Neon Error: {e}")
            QMessageBox.critical(self, "Neon Error", str(e))

    # ---- REC ----
    def on_start_recording(self):
        if not self.device or not self.gaze_thread:
            QMessageBox.information(self, "Calibration", "Connect Neon first.")
            return
        if not (self.tcp_thread and self.tcp_thread.conn):
            QMessageBox.information(self, "Calibration", "Start TCP server and connect the Quest app first.")
            return
        if self.state.recording:
            QMessageBox.information(self, "Recording", "Already recording.")
            return
        self.ensure_dirs()
        with self.state.lock:
            self.state.gaze_log.clear()
            self.state.datum_lines.clear()
        self.state.step = 0
        self.state.ended = False
        self.state.recording = True
        self.step_label.setText("Step: 1 / 25 — fixate, then Next")
        self.btn_start_calib.setEnabled(False)
        self.btn_next_step.setEnabled(True)
        if self.tcp_thread and self.tcp_thread.conn:
            self.tcp_thread.send_reset_calib()
            self.tcp_thread.send_step(0)

    def on_reset_calibration(self):
        with self.state.lock:
            self.state.gaze_log.clear()
            self.state.datum_lines.clear()
        self.state.step = 0
        self.state.ended = False
        self.state.recording = False
        self.step_label.setText("Step: idle")
        self.btn_start_calib.setEnabled(True)
        self.btn_next_step.setEnabled(False)
        if self.tracking_active:
            self.on_toggle_gaze_tracking()
        if self.eval_active:
            self.on_toggle_evaluation()
        if self.tcp_thread and self.tcp_thread.conn:
            self.tcp_thread.send_reset_calib()

    # ---- TCP ----
    def on_start_tcp(self):
        if self.tcp_thread and self.tcp_thread.is_alive():
            if self.tcp_thread.conn:
                self.status_tcp("already connected")
            else:
                from ..core.networking import HOST, PORT
                self.status_tcp(f"Waiting...{HOST}:{PORT}")
            return
        self.tcp_thread = TcpServer(self.status_tcp, message_cb=self.on_tcp_message)
        self.tcp_thread.start()

    def _neon_time_offset_estimate(self) -> Optional[Dict]:
        """Pupil Time Echo: phone→PC offset (same formula as Quest leg)."""
        if not self.device:
            return None
        try:
            est = self.device.estimate_time_offset(
                number_of_measurements=20,
                sleep_between_measurements_seconds=None,
            )
        except Exception as e:
            print(f"[timeEcho] neon estimate error: {e}")
            return None
        if est is None:
            return None
        return {
            "offset_phone_to_pc_ns": int(round(est.time_offset_ms.mean * 1_000_000)),
            "phone_offset_ms_mean": float(est.time_offset_ms.mean),
            "phone_offset_ms_std": float(est.time_offset_ms.std),
            "phone_rtt_ms_mean": float(est.roundtrip_duration_ms.mean),
        }

    def on_toggle_time_echo(self):
        mon = self._time_echo_monitor
        if mon is not None:
            try:
                alive = mon.is_alive()
            except TypeError:
                # Old bug: monitor named Event `_stop` and broke Thread.is_alive().
                alive = False
            if alive:
                mon.stop()
                mon.join(timeout=2.0)
                self._time_echo_monitor = None
                self.btn_time_echo.setText("Start Quest↔PC time-echo")
                self.status_sync("stopped")
                return
            self._time_echo_monitor = None

        if not (self.tcp_thread and self.tcp_thread.conn):
            QMessageBox.information(
                self,
                "Time echo",
                "Start TCP and connect MainStudy on Quest first.",
            )
            return

        self.ensure_dirs()
        period_s = float(self.sync_period_spin.value())
        self._time_echo_monitor = QuestTimeEchoMonitor(
            self.tcp_thread,
            participant_dir_fn=lambda: self.participant_dir,
            period_s=period_s,
            burst_n=100,
            rolling_n=100,
            neon_estimate_fn=self._neon_time_offset_estimate,
            neon_every_n_periods=max(5, int(10 / max(period_s, 0.5))),
            status_cb=self.status_sync,
        )
        self._time_echo_monitor.start()
        self.btn_time_echo.setText("Stop Quest↔PC time-echo")
        self.status_sync(f"running (period={period_s:.0f}s)")

    def on_tcp_message(self, msg: dict):
        # Runs on the TCP thread; marshal to GUI thread via signals.
        mtype = msg.get("type", "") if isinstance(msg, dict) else ""
        if mtype == "syncPulse":
            payload = msg.get("payload") or {}
            recv_ns = time.time_ns()
            self._sync_pulse_record.emit({
                "type": "syncPulse",
                "seq": payload.get("seq"),
                "quest_sent_unix_ms": payload.get("quest_sent_unix_ms"),
                "pc_recv_unix_ms": recv_ns // 1_000_000,
                "pc_recv_unix_ns": recv_ns,
            })
            return
        if mtype == "nextStep":
            # Only honor pinch during active calibration; ignore stray pinches.
            if self.state.recording and not self.state.ended:
                self._remote_next_step.emit()
            return
        if mtype == "mainStudyDone":
            payload = msg.get("payload") or {}
            self._main_study_done.emit(dict(payload))

    def _on_main_study_done(self, payload: dict):
        self._ms_running = False
        cond = payload.get("condition", "?")
        ok = payload.get("ok", True)
        sub = payload.get("sub_num", "?")
        subsub = payload.get("subsub_num", "?")
        reps = payload.get("reps", "?")
        state = "done" if ok else "failed"
        self.ms_status.setText(
            f"Status: {state} — sub {sub}-{subsub} {cond} ×{reps}; IDLE again"
        )
        self.btn_ms_start.setEnabled(True)
        print(f"[mainStudyDone] {payload}")

    def _on_sync_pulse_record(self, record: dict):
        # Prefer Companion HTTP /api/event so we do NOT call device.send_event
        # (asyncio.run) while GazeCollector is using the same Device — that race
        # can stall gaze streaming and corrupt later calibrations.
        if self.device:
            try:
                neon_ns = self._neon_companion_send_event(
                    "quest_sync", int(record["pc_recv_unix_ns"])
                )
                record["neon_event_name"] = "quest_sync"
                record["neon_event_ns"] = int(neon_ns)
                record["neon_event_ok"] = True
            except Exception as e:
                record["neon_event_ok"] = False
                record["neon_event_error"] = str(e)
                print(f"[syncPulse] companion event failed: {e}")
        else:
            record["neon_event_ok"] = False
            record["neon_event_error"] = "neon_not_connected"
            print("[syncPulse] Neon not connected; skipped send_event")

        self._ensure_sync_pulse_logger()
        self._sync_pulse_logger.log(record)
        print(
            f"[syncPulse] seq={record['seq']} quest_ms={record['quest_sent_unix_ms']} "
            f"pc_ms={record['pc_recv_unix_ms']} neon_ns={record.get('neon_event_ns')}"
        )

        if record.get("neon_event_ok"):
            self._sync_pulse_batch.append(record)
            if record.get("seq") == 2:
                self._finalize_sync_json()

    def _neon_companion_send_event(self, name: str, timestamp_ns: int) -> int:
        """POST /api/event on Neon Companion (phone). Avoids Device.send_event races."""
        phone_ip = getattr(self.device, "phone_ip", None)
        if not phone_ip:
            raise RuntimeError("device.phone_ip unavailable")
        url = f"http://{phone_ip}:8080/api/event"
        body = json.dumps(
            {"name": str(name), "timestamp": int(timestamp_ns)},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise RuntimeError(f"HTTP {e.code}: {detail or e.reason}") from e
        if raw.strip():
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("timestamp") is not None:
                    return int(payload["timestamp"])
            except Exception:
                pass
        return int(timestamp_ns)

    def _finalize_sync_json(self):
        payload = compute_sync_from_pulses(self._sync_pulse_batch)
        self._sync_pulse_batch.clear()
        if payload is None:
            print("[syncPulse] no valid neon pairs; sync.json not written")
            return
        self.ensure_dirs()
        self.sync_json_path = os.path.join(self.participant_dir, "sync.json")
        write_sync_json(self.sync_json_path, payload)
        print(
            f"[syncPulse] sync.json offset_quest_to_phone_ns="
            f"{payload['offset_quest_to_phone_ns']} "
            f"spread_std_ns={payload['offset_spread_std_ns']} "
            f"pulses={payload['pulse_count']}"
        )

    # ---- Evaluation ----
    def start_evaluation_logging(self):
        self.ensure_dirs()
        self.eval_dir = os.path.join(self.participant_dir, "evaluation")
        os.makedirs(self.eval_dir, exist_ok=True)
        self.eval_log_path = os.path.join(self.eval_dir, "evaluation_log.jsonl")
        self.state.start_evaluation(log_path=self.eval_log_path, models=self.models, canvas_w=CANVAS_W, canvas_h=CANVAS_H)
    
    def stop_evaluation_logging(self):
        self.state.stop_evaluation()

    def on_toggle_evaluation(self):
        if not self.device or not self.gaze_thread:
            QMessageBox.information(self, "Info", "Connect Neon")
            return
        if not (self.tcp_thread and self.tcp_thread.conn):
            QMessageBox.information(self, "Info", "Start TCP server")
            return
    
        if not self.eval_active:
            self.load_models()
            self.start_evaluation_logging()
            plan_path = self.build_and_save_random_saccade_plan()
            self.plan_streamer = EvalPlanStreamer(self.tcp_thread, plan_path, tick_hz=EVAL_RATE_HZ)
            self.plan_streamer.start()
            self.eval_active = True
            self.btn_eval.setText("Stop")
        else:
            self.stop_evaluation_logging()
            if self.plan_streamer:
                try: self.plan_streamer.stop()
                except Exception: pass
                self.plan_streamer = None
            self.eval_active = False
            self.btn_eval.setText("Start Evaluation")
    
    def start_gaze_logging(self):
        self.ensure_dirs()
        self.gaze_tracking_dir = os.path.join(self.participant_dir, "gaze_tracking")
        os.makedirs(self.gaze_tracking_dir, exist_ok=True)
        self.gaze_log_path = os.path.join(self.gaze_tracking_dir, "gaze_log.jsonl")
        self.state.start_tracking(log_path=self.gaze_log_path, models=self.models, canvas_w=CANVAS_W, canvas_h=CANVAS_H)
    
    def stop_gaze_logging(self):
        self.state.stop_tracking()

    def on_toggle_gaze_tracking(self):
        if not self.device or not self.gaze_thread:
            QMessageBox.information(self, "Info", "Connect Neon")
            return
        if not (self.tcp_thread and self.tcp_thread.conn):
            QMessageBox.information(self, "Info", "Start TCP server")
            return
        
        if not self.tracking_active:
            if not self.load_models():
                return
            self.visualize.setChecked(True)
            self.start_gaze_logging()
            self.tracking_active = True
            self.btn_track.setText("Stop")
            self._start_gaze_tx()
        else:
            self.stop_gaze_logging()
            self.tracking_active = False
            self.btn_track.setText("Start Gaze Tracking")
            self._stop_gaze_tx()
            with self._gv_lock:
                self._gv_latest_raw = None

    # ---- Study handoff ----
    def on_launch_practice(self):
        if not (self.tcp_thread and self.tcp_thread.conn):
            QMessageBox.information(
                self, "Start Practice",
                "Start the TCP server and connect the Quest app first.",
            )
            return

        confirm = QMessageBox.question(
            self, "Start Practice",
            "Launch PracticeTask (auto Fitts) on the Quest?\n"
            "Package: com.PracticeMG.MRstressPRACTICE",
        )
        if confirm != QMessageBox.Yes:
            return

        if self.eval_active:
            self.on_toggle_evaluation()

        self.tcp_thread.send_launch_app("com.PracticeMG.MRstressPRACTICE")
        self.step_label.setText("Step: launching PracticeTask on Quest...")

    def on_launch_study(self):
        if not (self.tcp_thread and self.tcp_thread.conn):
            QMessageBox.information(self, "Start Main Study", "Start the TCP server and connect the Quest app first.")
            return

        confirm = QMessageBox.question(
            self, "Start Main Study",
            "Launch MainStudy (PC-commanded) on the Quest and close OpenEye there?\n"
            "Make sure calibration looks good first.\n"
            "Package: com.PracticeMG.MRstress",
        )
        if confirm != QMessageBox.Yes:
            return

        # Stop the calibration-eval saccade stream, but KEEP gaze tracking running:
        # after MainStudy reconnects, the PC auto-resumes streaming gazeVisual to it.
        if self.eval_active:
            self.on_toggle_evaluation()

        if not self.tracking_active:
            QMessageBox.information(
                self, "Start Main Study",
                "Note: Gaze Tracking is OFF. Eye blocks in MainStudy will have no gaze "
                "until you press 'Start Gaze Tracking' (with Visualize) here after it connects."
            )

        self.tcp_thread.send_launch_app("com.PracticeMG.MRstress")
        self.ms_status.setText("Status: launching MainStudy — wait for Quest IDLE")
        self.step_label.setText("Step: launching MainStudy on Quest (gaze streaming stays on)...")

    def on_main_study_start(self):
        if not (self.tcp_thread and self.tcp_thread.conn):
            QMessageBox.information(
                self, "Main Study",
                "Quest MainStudy must be connected on TCP first (IDLE screen).",
            )
            return
        if self._ms_running:
            QMessageBox.information(
                self, "Main Study",
                "A condition is already running. Wait for mainStudyDone (or restart Quest).",
            )
            return

        condition = self.ms_condition.currentText()
        sub_num = int(self.ms_sub.value())
        subsub_num = int(self.ms_subsub.value())
        reps = int(self.ms_reps.value())
        duration_sec = int(self.ms_duration_min.value()) * 60

        confirm = QMessageBox.question(
            self, "Start condition",
            f"Start on Quest?\n\n"
            f"Participant {sub_num}-{subsub_num}\n"
            f"Condition: {condition}\n"
            f"Fitts reps: {reps}\n"
            f"Each rep duration: {duration_sec // 60} min ({duration_sec}s)\n\n"
            f"Quest returns to IDLE when finished.",
        )
        if confirm != QMessageBox.Yes:
            return

        if not self.tracking_active and condition.startswith("Eye"):
            QMessageBox.information(
                self, "Main Study",
                "Gaze Tracking is OFF. Turn it on (and Visualize) for eye conditions.",
            )

        self.tcp_thread.send_main_study_start(
            sub_num=sub_num,
            subsub_num=subsub_num,
            condition=condition,
            reps=reps,
            duration_sec=duration_sec,
        )
        self._ms_running = True
        self.btn_ms_start.setEnabled(False)
        self.ms_status.setText(
            f"Status: running {condition} ×{reps} ({duration_sec // 60} min each) "
            f"for {sub_num}-{subsub_num}…"
        )

    def on_recalibrate(self):
        if not (self.tcp_thread and self.tcp_thread.conn):
            QMessageBox.information(
                self, "Recalibrate",
                "Start the TCP server and connect MainStudy on the Quest first.",
            )
            return

        confirm = QMessageBox.question(
            self, "Recalibrate",
            "Launch the OpenEye calibration app on the Quest and close MainStudy?\n"
            "After calibration, use Start Study to return to MainStudy.",
        )
        if confirm != QMessageBox.Yes:
            return

        if self.eval_active:
            self.on_toggle_evaluation()
        if self.tracking_active:
            self.on_toggle_gaze_tracking()

        self.tcp_thread.send_launch_app("org.MixedRealityToolkit.MRTK3Sample")
        self.step_label.setText("Step: launching OpenEye on Quest for recalibration...")

    def load_models(self):
        model_dir = os.path.join(self.participant_dir, "models")
        if not os.path.isdir(model_dir):
            QMessageBox.warning(self, "Models", f"Models folder not found: {model_dir}")
            return False
        self.models = load_models(model_dir)
        if not self.models:
            QMessageBox.warning(self, "Models", "No valid model JSON found in Models directory.")
            return False
        return True
    
    # ---- Random Saccade Function ----
    def _deg_to_m(self, deg_x: float, deg_y: float, distance_m: float=1.0):
        rad_x = np.deg2rad(deg_x)
        rad_y = np.deg2rad(deg_y)
        x = distance_m * np.tan(rad_x)
        y = distance_m * np.tan(rad_y)
        return float(x), float(y)

    def _sample_random_deg(self, prev_deg: Optional[tuple[float, float]]=None):
        hx0, hx1 = H_RANGE_DEG
        vy0, vy1 = V_RANGE_DEG
        for _ in range(1000):
            dx = np.random.uniform(hx0, hx1)
            dy = np.random.uniform(vy0, vy1)
            if prev_deg is None:
                return float(dx), float(dy)
            dist = np.hypot(dx - prev_deg[0], dy - prev_deg[1])
            if dist >= CFG.eval.min_saccade_amp_deg:
                return float(dx), float(dy)
        return float(dx), float(dy)
    
    def build_and_save_random_saccade_plan(self):
        self.ensure_dirs()

        frames_total = int(EVAL_DURATION_S * EVAL_RATE_HZ)
        frames_per_target = max(1, int(round(EVAL_DWELL_MS * EVAL_RATE_HZ / 1000)))

        timeline = []
        t_ms = 0
        frame_interval_ms = int(round(1000 / EVAL_RATE_HZ))

        prev_deg = None
        ti = 0
        block_idx = 0

        while ti < frames_total:
            if block_idx == 0: deg_x, deg_y = 0.0, 0.0
            elif ti + frames_per_target >= frames_total: deg_x, deg_y = 0.0, 0.0
            else: deg_x, deg_y = self._sample_random_deg(prev_deg)

            x_m, y_m = self._deg_to_m(deg_x, deg_y, distance_m=1.0)

            for _ in range(frames_per_target):
                if ti >= frames_total:
                    break
                timeline.append({
                    "t_ms": int(t_ms),
                    "pos": {"x": float(x_m), "y": float(y_m)}
                })
                ti += 1
                t_ms += frame_interval_ms

            prev_deg = (deg_x, deg_y)
            block_idx += 1

        plan = {
            "meta": {
                "rate_hz": EVAL_RATE_HZ,
                "dwell_ms": EVAL_DWELL_MS,
                "duration_s": EVAL_DURATION_S,
                "frames_total": frames_total,
            },
            "timeline": timeline
        }

        self.eval_dir = os.path.join(self.participant_dir, "evaluation")
        os.makedirs(self.eval_dir, exist_ok=True)
        self.plan_path = os.path.join(self.eval_dir, "random_saccade_plan.json")
        with open(self.plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        print(f"[EVAL] saved: {self.plan_path}")
        return self.plan_path

    def _gaze_event_count(self) -> int:
        with self.state.lock:
            return sum(1 for row in self.state.gaze_log if row[5] == "event_log")

    def _wait_for_gaze_event(self, prev_count: int, timeout_s: float = 1.0) -> bool:
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            if self._gaze_event_count() > prev_count:
                return True
            time.sleep(0.005)
        return False

    # ---- Step / End ----
    def trigger_step(self):
        if self.state.ended:
            return
        if not self.state.recording:
            QMessageBox.information(self, "Info", "Press 'S' to start recording first.")
            return

        prev_events = self._gaze_event_count()
        # Log gaze for the dot currently shown (step is 0-based dot index).
        self.state.set_event()

        if self.state.step < 24:
            self.state.step += 1
            if self.tcp_thread and self.tcp_thread.conn:
                self.tcp_thread.send_step(self.state.step)
            self.step_label.setText(f"Step: {self.state.step + 1} / 25 — fixate, then Next")
        else:
            if not self._wait_for_gaze_event(prev_events):
                print("[CALIB] timed out waiting for final gaze event")
            if self.tcp_thread and self.tcp_thread.conn:
                self.tcp_thread.send_end_signal()
            self.state.ended = True
            model_dir = self.save_csv()
            self.state.recording = False
            self.btn_start_calib.setEnabled(True)
            self.btn_next_step.setEnabled(False)
            self.step_label.setText("Step: done")
            if model_dir:
                QMessageBox.information(
                    self, "Calibration",
                    f"Calibration complete.\nModels saved to:\n{model_dir}",
                )
            else:
                QMessageBox.warning(
                    self, "Calibration",
                    "Calibration finished but models were NOT saved.\n"
                    "Check the terminal for [CALIB] errors (need 25 gaze events).",
                )

    # ---- Save ----
    def save_csv(self):
        if not self.calib_dir:
            self.ensure_dirs()

        model_path = None
        gaze_rows = self.state.snapshot_gaze_log()
        if gaze_rows:
            df = pd.DataFrame(
                gaze_rows,
                columns=["timestamp", "gaze_x", "gaze_y", "raw_gaze_x", "raw_gaze_y", "event_log"]
            )
            calib_csv = os.path.join(self.calib_dir, "calibration_log.csv")
            df.to_csv(calib_csv, index=False)
        else:
            calib_csv = None

        pairs_csv = self.build_mapping(calib_csv) if calib_csv else None
        if pairs_csv:
            model_path = self.build_and_save_models(pairs_csv)
        else:
            print("[CALIB] calibration_pair.csv not created — models skipped")

        datum_path = os.path.join(self.calib_dir, "raw_neon_data.jsonl")
        with self.state.lock:
            lines = list(self.state.datum_lines)
        if lines:
            with open(datum_path, "wt", encoding="utf-8") as f:
                for line in lines:
                    f.write(line+"\n")

        return model_path

    # ---- Mapping ----
    def build_mapping(self, calib_csv_path: str, window: int=CFG.mapping.window_width):
        if not os.path.exists(calib_csv_path):
            return None
        df = pd.read_csv(calib_csv_path)
        ev = df["event_log"].astype(str).fillna("")
        event_idxs = list(np.flatnonzero(ev == "event_log"))
        if len(event_idxs) < 25:
            print(f"[CALIB] expected 25 gaze events, found {len(event_idxs)}")
            return None
        event_idxs = event_idxs[:25]

        rows = []
        for seq_idx, i in enumerate(event_idxs):
            start = max(0, i - window)
            seg = df.iloc[start:i]
            if len(seg) == 0:
                seg = df.iloc[i:i+1]
            avg_gx, avg_gy = float(seg["gaze_x"].mean()), float(seg["gaze_y"].mean())
            target_x, target_y = CALIB_TARGET_COORDINATES[seq_idx]
            rows.append({
                "step": seq_idx + 1,
                "target_x": target_x,
                "target_y": target_y,
                "avg_gaze_x": avg_gx,
                "avg_gaze_y": avg_gy
            })
        out_df = pd.DataFrame(rows)
        out_path = os.path.join(self.calib_dir, "calibration_pair.csv")
        out_df.to_csv(out_path, index=False)
        return out_path
    
    def build_and_save_models(self, pair_csv_path: str):
        df = pd.read_csv(pair_csv_path)
        neon_xy_raw = df[["avg_gaze_x", "avg_gaze_y"]].to_numpy(float)
        vp_xy = df[["target_x", "target_y"]].to_numpy(float)

        neon_xy = normalize_neon_xy(neon_xy_raw, CANVAS_W, CANVAS_H)
        canvas_size = (CANVAS_W, CANVAS_H)

        biquad_model = map_biquadratic(neon_xy, vp_xy, canvas_size)
        ridge_model = map_ridge_biquadratic(neon_xy, vp_xy, canvas_size, alpha=CFG.mapping.ridge_alpha)

        model_path = os.path.join(self.participant_dir, "models")
        save_models(model_path, biquad_model, ridge_model)
        
        return model_path

    # ---- Cleanup ----
    def cleanup_and_close(self):
        self.state.running = False
        try:
            if self._time_echo_monitor and self._time_echo_monitor.is_alive():
                self._time_echo_monitor.stop()
                self._time_echo_monitor.join(timeout=2.0)
                self._time_echo_monitor = None
        except Exception:
            pass
        time.sleep(0.1)
        try:
            if self.device:
                self.device.streaming_stop("gaze")
                self.device.close()
        except Exception: pass
        try:
            if self.tcp_thread:
                self.tcp_thread.close()
        except Exception: pass
        self.close()

# ==============================
# Main
# ==============================

def main(argv=None) -> int:
    
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(800, 500)
    w.show()
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())