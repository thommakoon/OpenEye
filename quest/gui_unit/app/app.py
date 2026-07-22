import argparse
import sys
import os
import threading
import time
import json
from pathlib import Path
from typing import Optional, List, Dict

import pandas as pd
import numpy as np
# ---- PySide6 GUI ----
from PySide6.QtCore import Qt, QTimer, QPointF, Signal, QSettings
from PySide6.QtGui import QPixmap, QPainter, QPen, QColor, QFont, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QCheckBox, QSpinBox, QComboBox, QGroupBox, QMessageBox,
    QLineEdit
)
# ---- Pupil Labs Real-Time API ----
from pupil_labs.realtime_api.simple import discover_one_device

# ==============================
# Local
# ==============================
from ..core.config import DEFAULT_CONFIG as CFG
from ..core.session import (
    TrialSession,
    GazeMode,
    trial_id_from_num,
    PKG_OPENEYE,
    PKG_PRACTICE,
    PKG_MAIN_STUDY,
)
from ..core.networking import TcpServer, EvalPlanStreamer
from ..core.urp2026_client import send_command as urp_send_command, Urp2026Error
from ..core.filter import ButterLPFilter
from ..core.logger import JsonLogger
from ..core.time_echo_monitor import PcHubSyncMonitor
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
    _session_hello = Signal(dict)
    _urp_result = Signal(str, int, str, bool)  # command, http_code, body, ok

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Quest-Neon Calibration")
        self._settings = QSettings("OpenEye", "QuestNeonGUI")
        self.session = TrialSession()
        self._want_study_gaze = False  # sticky: survive brief TCP drop after handoff
        self._gaze_watchdog: Optional[QTimer] = None
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

        self.gv_rate_hz = 60  # viz / Quest: latest-wins; 100Hz JSON easily backlogged on HMD
        self._gv_lock = threading.Lock()
        self._gv_latest_raw = None
        self._gaze_tx_thread: Optional[GazeTxThread] = None
        self._sync_pulse_logger: Optional[JsonLogger] = None
        self.sync_pulses_path = None
        self.sync_json_path = None
        self._sync_pulse_batch: List[dict] = []
        self._time_echo_monitor: Optional[PcHubSyncMonitor] = None
        self._quest_echo_logger: Optional[JsonLogger] = None
        self._neon_echo_logger: Optional[JsonLogger] = None
        self._hub_sync_user_stopped = False
        self._calib_dot_shown_at = 0.0
        self._calib_dwell_timer = QTimer(self)
        self._calib_dwell_timer.setSingleShot(True)
        self._calib_dwell_timer.timeout.connect(self._on_calib_dwell_ready)

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

        self.session_status = QLabel("Session: (select trial)")
        self.session_status.setWordWrap(True)

        calib_box = QGroupBox("Calibration"); calib_layout = QHBoxLayout(calib_box)
        self.btn_start_calib = QPushButton("Start Calibration (S)")
        self.btn_next_step = QPushButton("Next Step (G/H/I/J)")
        self.btn_reset_calib = QPushButton("Reset Calibration")
        self.btn_next_step.setEnabled(False)
        self.calib_hint = QLabel(
            "1. Connect Neon  2. Start TCP  3. Open calib APK on Quest (manual)  "
            "4. Start Calibration  5. Fixate each dot ≥1s, then Next (G/H/I/J) ×25"
        )
        calib_layout.addWidget(self.btn_start_calib)
        calib_layout.addWidget(self.btn_next_step)
        calib_layout.addWidget(self.btn_reset_calib)
        calib_layout.addWidget(self.calib_hint, stretch=1)

        self.canvas = GazeCanvas()
        task_box = QGroupBox("Tasks"); task_layout = QHBoxLayout(task_box)

        self.btn_eval = QPushButton("Start Evaluation")
        self.btn_track = QPushButton("Start Gaze Tracking")
        self.visualize = QCheckBox("Visualize")
        self.chk_show_ray = QCheckBox("Show Quest ray")
        self.chk_show_ray.setChecked(True)
        self.chk_show_ray.setToolTip(
            "Toggle eye/head/hand ray visuals on the connected Practice or MainStudy app."
        )
        task_layout.addWidget(self.btn_eval)
        task_layout.addWidget(self.btn_track)
        task_layout.addWidget(self.visualize)
        task_layout.addWidget(self.chk_show_ray)

        # Option 1 (pilot): APK launchApp handoff is off the critical path.
        # Open Calib / Practice / Main manually on Quest; PC = TCP + gaze + commander.
        # Debug checkbox re-enables the old launchApp buttons only.
        study_box = QGroupBox("Study apps (manual launch — no APK transfer)")
        study_layout = QVBoxLayout(study_box)
        self.study_hint = QLabel(
            "Operator flow: on Quest open Calib → calibrate → quit → open Practice or Main Study. "
            "On PC: Connect Neon → Start TCP → Gaze Tracking / Visualize → "
            "(Main) use commander below. Do not rely on APK transfer."
        )
        self.study_hint.setWordWrap(True)
        self.chk_debug_handoff = QCheckBox("Debug: show APK handoff buttons (launchApp)")
        self.chk_debug_handoff.setChecked(False)
        handoff_row = QHBoxLayout()
        self.btn_launch_practice = QPushButton("Start Practice")
        self.btn_launch_study = QPushButton("Start Main Study")
        self.btn_recalibrate = QPushButton("Recalibrate (OpenEye)")
        handoff_row.addWidget(self.btn_launch_practice)
        handoff_row.addWidget(self.btn_launch_study)
        handoff_row.addWidget(self.btn_recalibrate)
        self._handoff_buttons = (
            self.btn_launch_practice,
            self.btn_launch_study,
            self.btn_recalibrate,
        )
        study_layout.addWidget(self.study_hint)
        study_layout.addWidget(self.chk_debug_handoff)
        study_layout.addLayout(handoff_row)
        self._set_handoff_ui_visible(False)

        main_box = QGroupBox("Main Study commander (MRstress IDLE)")
        main_layout = QHBoxLayout(main_box)
        self.ms_sub = QSpinBox(); self.ms_sub.setRange(0, 999); self.ms_sub.setValue(0)
        self.ms_subsub = QSpinBox(); self.ms_subsub.setRange(0, 9); self.ms_subsub.setValue(0)
        self.ms_condition = QComboBox()
        self.ms_condition.addItems(["HeadPinch", "HandPinch", "EyePinch"])
        self.ms_reps = QSpinBox(); self.ms_reps.setRange(1, 10); self.ms_reps.setValue(3)
        self.ms_duration_min = QSpinBox(); self.ms_duration_min.setRange(1, 30); self.ms_duration_min.setValue(5)
        self.ms_duration_min.setSuffix(" min")
        self.btn_ms_start = QPushButton("Start condition")
        self.btn_ms_stop = QPushButton("Stop → IDLE")
        self.ms_status = QLabel(
            "Status: idle — open MainStudy APK on Quest manually, then Start condition"
        )
        main_layout.addWidget(QLabel("sub")); main_layout.addWidget(self.ms_sub)
        main_layout.addWidget(QLabel("subsub")); main_layout.addWidget(self.ms_subsub)
        main_layout.addWidget(QLabel("condition")); main_layout.addWidget(self.ms_condition)
        main_layout.addWidget(QLabel("reps")); main_layout.addWidget(self.ms_reps)
        main_layout.addWidget(QLabel("each")); main_layout.addWidget(self.ms_duration_min)
        main_layout.addWidget(self.btn_ms_start)
        main_layout.addWidget(self.btn_ms_stop)
        main_layout.addWidget(self.ms_status, stretch=1)

        sync_box = QGroupBox("Clock sync (PC hub — Quest + Neon time-echo)")
        sync_layout = QHBoxLayout(sync_box)
        self.btn_time_echo = QPushButton("Start PC hub sync")
        self.sync_period_spin = QSpinBox()
        self.sync_period_spin.setRange(1, 60)
        self.sync_period_spin.setValue(1)
        self.sync_period_spin.setSuffix(" s")
        self.sync_status = QLabel("Sync: idle")
        sync_layout.addWidget(self.btn_time_echo)
        sync_layout.addWidget(QLabel("period"))
        sync_layout.addWidget(self.sync_period_spin)
        sync_layout.addWidget(self.sync_status, stretch=1)

        urp_box = QGroupBox("URP2026 recorder (QT Py + both-recording via phone bridge :8765)")
        urp_layout = QVBoxLayout(urp_box)

        urp_conn = QHBoxLayout()
        self.urp_host = QLineEdit()
        self.urp_host.setPlaceholderText("phone IP (URP2026 → Start PC bridge)")
        self.urp_port = QSpinBox(); self.urp_port.setRange(1, 65535)
        self.urp_port.setValue(int(self._settings.value("urp2026/port", CFG.urp2026.port, type=int)))
        self.urp_host.setText(str(self._settings.value("urp2026/host", CFG.urp2026.default_host)))
        self.btn_urp_health = QPushButton("Health")
        urp_conn.addWidget(QLabel("host")); urp_conn.addWidget(self.urp_host, stretch=1)
        urp_conn.addWidget(QLabel("port")); urp_conn.addWidget(self.urp_port)
        urp_conn.addWidget(self.btn_urp_health)

        urp_cmds = QHBoxLayout()
        self.btn_urp_calibrate = QPushButton("Calibrate")
        self.btn_urp_start = QPushButton("Start")
        self.btn_urp_stop = QPushButton("Stop")
        self.btn_urp_next = QPushButton("Next")
        self.btn_urp_status = QPushButton("Status")
        self.btn_urp_rec_start = QPushButton("Record Start")
        self.btn_urp_rec_stop = QPushButton("Record Stop")
        for b in (self.btn_urp_calibrate, self.btn_urp_start, self.btn_urp_stop,
                  self.btn_urp_next, self.btn_urp_status,
                  self.btn_urp_rec_start, self.btn_urp_rec_stop):
            urp_cmds.addWidget(b)

        self.urp_status = QLabel("URP2026: idle"); self.urp_status.setWordWrap(True)
        urp_layout.addLayout(urp_conn)
        urp_layout.addLayout(urp_cmds)
        urp_layout.addWidget(self.urp_status)

        self._urp_buttons = [
            self.btn_urp_health, self.btn_urp_calibrate, self.btn_urp_start,
            self.btn_urp_stop, self.btn_urp_next, self.btn_urp_status,
            self.btn_urp_rec_start, self.btn_urp_rec_stop,
        ]

        root.addWidget(ctrl_box); root.addWidget(self.session_status)
        root.addWidget(calib_box); root.addWidget(self.canvas)
        root.addWidget(task_box); root.addWidget(study_box); root.addWidget(main_box); root.addWidget(sync_box)
        root.addWidget(urp_box)

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
        self._session_hello.connect(self._on_session_hello)
        self.p_spin.valueChanged.connect(self.on_trial_changed)
        self.visualize.toggled.connect(self.on_visualize_toggled)
        self.chk_show_ray.toggled.connect(self.on_show_ray_toggled)
        self.btn_time_echo.clicked.connect(self.on_toggle_time_echo)
        self.btn_eval.clicked.connect(self.on_toggle_evaluation)
        self.btn_track.clicked.connect(self.on_toggle_gaze_tracking)
        self.btn_launch_practice.clicked.connect(self.on_launch_practice)
        self.btn_launch_study.clicked.connect(self.on_launch_study)
        self.btn_ms_start.clicked.connect(self.on_main_study_start)
        self.btn_ms_stop.clicked.connect(self.on_main_study_stop)
        self.btn_recalibrate.clicked.connect(self.on_recalibrate)
        self.chk_debug_handoff.toggled.connect(self._set_handoff_ui_visible)
        self._ms_running = False

        # URP2026 recorder wiring
        self.btn_urp_health.clicked.connect(lambda: self._urp_send("health"))
        self.btn_urp_calibrate.clicked.connect(lambda: self._urp_send("calibrate"))
        self.btn_urp_start.clicked.connect(lambda: self._urp_send("start"))
        self.btn_urp_stop.clicked.connect(lambda: self._urp_send("stop"))
        self.btn_urp_next.clicked.connect(lambda: self._urp_send("next"))
        self.btn_urp_status.clicked.connect(lambda: self._urp_send("status"))
        self.btn_urp_rec_start.clicked.connect(lambda: self._urp_send("record_start"))
        self.btn_urp_rec_stop.clicked.connect(lambda: self._urp_send("record_stop"))
        self._urp_result.connect(self._on_urp_result)
        self.urp_host.editingFinished.connect(self._save_urp_settings)
        self.urp_port.valueChanged.connect(self._save_urp_settings)

        # UI update timer
        self.timer = QTimer(self); self.timer.timeout.connect(self.repaint_canvas); self.timer.start(5)

        # shortcut
        self.setFocusPolicy(Qt.StrongFocus)

        # Next calib dot: G / H / I / J (Space removed — easier while standing / walking pad)
        self._shortcut_next_step = []
        for key in (Qt.Key_G, Qt.Key_H, Qt.Key_I, Qt.Key_J):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(self.trigger_step)
            self._shortcut_next_step.append(sc)

        self.shortcut_s = QShortcut(QKeySequence(Qt.Key_S), self)
        self.shortcut_s.setContext(Qt.ApplicationShortcut)
        self.shortcut_s.activated.connect(self.on_start_recording)

        self.shortcut_e = QShortcut(QKeySequence(Qt.Key_E), self)
        self.shortcut_e.setContext(Qt.ApplicationShortcut)
        self.shortcut_e.activated.connect(self.on_toggle_evaluation)

        self.shortcut_q = QShortcut(QKeySequence(Qt.Key_Q), self)
        self.shortcut_q.setContext(Qt.ApplicationShortcut)
        self.shortcut_q.activated.connect(self.cleanup_and_close)

        self.on_trial_changed(self.p_spin.value())

        self._gaze_watchdog = QTimer(self)
        self._gaze_watchdog.setInterval(500)
        self._gaze_watchdog.timeout.connect(self._auto_resume_study_gaze)
        self._gaze_watchdog.start()

    # ---- Session / gaze state machine ----
    def on_trial_changed(self, p_num: int):
        self.session.set_trial(trial_id_from_num(int(p_num)))
        self.ensure_dirs()
        self._refresh_session_ui()

    def _refresh_session_ui(self):
        self.session_status.setText(f"Session: {self.session.format_line()}")
        # Visualize = "gaze stream ON" for operator feedback.
        want_viz = self.session.gaze != GazeMode.OFF
        if self.visualize.isChecked() != want_viz:
            self.visualize.blockSignals(True)
            self.visualize.setChecked(want_viz)
            self.visualize.blockSignals(False)
        self.btn_track.setText("Stop" if self.session.gaze != GazeMode.OFF else "Start Gaze Tracking")

    def _set_gaze_mode(self, mode: GazeMode, *, quiet: bool = False) -> bool:
        """
        Single entry for PC→Quest gazeVisual stream.
          OFF          — stop TX (required before handoff)
          OPENEYE_VIZ  — stream + Visualize checkbox (OpenEye Dot / ray)
          STUDY        — stream without OpenEye viz (Practice / MainStudy cursor)
        """
        if mode == GazeMode.OFF:
            self._stop_gaze_tx()
            if self.tracking_active:
                self.stop_gaze_logging()
                self.tracking_active = False
            with self._gv_lock:
                self._gv_latest_raw = None
            self.session.set_gaze(GazeMode.OFF)
            self._refresh_session_ui()
            return True

        if not self.device or not self.gaze_thread:
            if not quiet:
                QMessageBox.information(self, "Gaze", "Connect Neon first.")
            return False
        if not (self.tcp_thread and self.tcp_thread.conn):
            if not quiet:
                QMessageBox.information(self, "Gaze", "Quest must be connected on TCP.")
            return False

        self.ensure_dirs()
        if not self.load_models(quiet=quiet):
            return False

        if not self.tracking_active:
            self.start_gaze_logging()
            self.tracking_active = True
        if self._gaze_tx_thread is None or not self._gaze_tx_thread.is_alive():
            self._start_gaze_tx()

        self.session.set_gaze(mode)
        if mode == GazeMode.STUDY:
            self._want_study_gaze = True
        elif mode == GazeMode.OPENEYE_VIZ:
            self._want_study_gaze = False
        self._refresh_session_ui()
        print(f"[gaze] mode → {mode.value} (app={self.session.app})")
        return True

    def on_visualize_toggled(self, checked: bool):
        """Visualize checkbox is a real control — not a no-op flag."""
        if checked:
            if self.session.app in ("practice", "main_study") or self._want_study_gaze:
                ok = self._set_gaze_mode(GazeMode.STUDY)
            else:
                ok = self._set_gaze_mode(GazeMode.OPENEYE_VIZ)
            if not ok:
                self.visualize.blockSignals(True)
                self.visualize.setChecked(False)
                self.visualize.blockSignals(False)
            return

        self._want_study_gaze = False
        if self.session.gaze != GazeMode.OFF:
            self._set_gaze_mode(GazeMode.OFF)

    def on_show_ray_toggled(self, checked: bool):
        self._push_show_ray()

    def _push_show_ray(self):
        if not (self.tcp_thread and self.tcp_thread.conn):
            return
        try:
            visible = bool(self.chk_show_ray.isChecked())
            self.tcp_thread.send_show_ray(visible)
        except Exception as e:
            print(f"[showRay] send failed: {e}")

    def _set_handoff_ui_visible(self, visible: bool):
        """Show/hide launchApp transfer buttons (off by default for pilot reliability)."""
        for btn in getattr(self, "_handoff_buttons", ()):
            btn.setVisible(bool(visible))

    def go_to(self, target_package: str, *, title: str, detail: str) -> bool:
        """
        Debug-only APK handoff via launchApp.

        Critical path no longer uses this. When reimplemented, keep a thin contract:
          1) PC sends launchApp + expected package
          2) Target app sends sessionHello once on TCP connect
          3) PC resumes gaze only if hello.package matches expected
          4) Hard timeout + clear error (no silent fallback guessing)
        Do not expand sticky-session / fallback-hello until that contract is solid.
        """
        if not getattr(self, "chk_debug_handoff", None) or not self.chk_debug_handoff.isChecked():
            QMessageBox.information(
                self,
                title,
                "APK handoff is disabled.\n\n"
                "Open the target app manually on the Quest, then Connect TCP / Gaze on PC.\n"
                "Enable “Debug: show APK handoff buttons” only if you need launchApp.",
            )
            return False

        ok, reason = self.session.can_handoff_to(target_package)
        if not ok:
            QMessageBox.information(self, title, reason)
            return False

        confirm = QMessageBox.question(
            self, title,
            detail + "\n\nGaze will be forced OFF, then auto-resumed after the new app connects.",
        )
        if confirm != QMessageBox.Yes:
            return False

        if self.eval_active:
            self.on_toggle_evaluation()

        # Sticky intent — survives brief TCP gap / reconnect after launchApp.
        self._want_study_gaze = target_package in (PKG_PRACTICE, PKG_MAIN_STUDY)
        self._set_gaze_mode(GazeMode.OFF, quiet=True)
        time.sleep(0.15)

        self.tcp_thread.send_launch_app(target_package)
        self.session.note_handoff(target_package)
        self._refresh_session_ui()
        for delay_ms in (500, 1500, 3000, 5000):
            QTimer.singleShot(delay_ms, self._auto_resume_study_gaze)
        return True

    # ---- Helpers ----
    def _apply_tcp_status(self, msg: str):
        self.tcp_status.setText(f"TCP: {msg}")
        low = (msg or "").lower()
        if low.startswith("connected"):
            self.session.on_tcp_connected()
            print(
                f"[tcp] connected want_study_gaze={self._want_study_gaze} "
                f"pending={self.session.handoff_pending or '-'}"
            )
            if self.session.handoff_pending:
                QTimer.singleShot(400, self._fallback_hello_from_pending)
            for delay_ms in (200, 800, 2000):
                QTimer.singleShot(delay_ms, self._auto_resume_study_gaze)
            # Push current ray visibility after Quest reconnects.
            QTimer.singleShot(300, self._push_show_ray)
            QTimer.singleShot(500, self._maybe_auto_start_hub_sync)
        elif "disconnect" in low or low.startswith("recv error") or low.startswith("quest disconnect"):
            # Pause TX into dead socket, but KEEP _want_study_gaze.
            # Old bug: after fallback cleared handoff_pending, disconnect permanently
            # killed STUDY — only quit/reenter (new Connected without that kill) fixed it.
            if self.session.gaze != GazeMode.OFF:
                print(f"[tcp] disconnect — pausing TX (want_study={self._want_study_gaze})")
                self._stop_gaze_tx()
                self.session.set_gaze(GazeMode.OFF)
            self.session.on_tcp_disconnected()
        self._refresh_session_ui()

    def _fallback_hello_from_pending(self):
        pending = self.session.handoff_pending
        if not pending or not self.session.tcp_connected:
            return
        if self.session.app not in ("none", "unknown"):
            return
        self.session.on_session_hello(pending, "connected")
        print(f"[session] fallback hello from handoff_pending={pending}")
        self._refresh_session_ui()
        self._auto_resume_study_gaze()

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
        if self.session.gaze == GazeMode.OFF:
            return
        if not self.tracking_active:
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
            QTimer.singleShot(500, self._maybe_auto_start_hub_sync)

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
        self.session.on_calib_started()
        self.btn_start_calib.setEnabled(False)
        if self.tcp_thread and self.tcp_thread.conn:
            self.tcp_thread.send_reset_calib()
            self.tcp_thread.send_step(0)
        self._arm_calib_dwell()

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
        self._calib_dwell_timer.stop()
        if self.tracking_active:
            self.on_toggle_gaze_tracking()
        if self.eval_active:
            self.on_toggle_evaluation()
        if self.tcp_thread and self.tcp_thread.conn:
            self.tcp_thread.send_reset_calib()
        if self.session.model_ready:
            with self.session.lock:
                self.session.openeye = "ready"
        else:
            with self.session.lock:
                self.session.openeye = "no_model"
        self._refresh_session_ui()

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

    def _ensure_echo_loggers(self):
        self.ensure_dirs()
        if self._quest_echo_logger is None:
            path = os.path.join(self.participant_dir, "sync_quest_echo.jsonl")
            self._quest_echo_logger = JsonLogger(path)
            self._quest_echo_logger.start()
        if self._neon_echo_logger is None:
            path = os.path.join(self.participant_dir, "sync_neon_echo.jsonl")
            self._neon_echo_logger = JsonLogger(path)
            self._neon_echo_logger.start()

    def _log_quest_echo(self, sample: dict):
        self._ensure_echo_loggers()
        self._quest_echo_logger.log({"type": "questEcho", **sample})

    def _log_neon_echo(self, sample: dict):
        self._ensure_echo_loggers()
        self._neon_echo_logger.log({"type": "neonEcho", **sample})

    def _neon_time_echo_from_est(self, est, *, n: int) -> Optional[Dict]:
        if est is None:
            return None
        offset_ms = float(est.time_offset_ms.mean)
        return {
            "offset_ms": offset_ms,
            "offset_phone_to_pc_ns": int(round(offset_ms * 1_000_000)),
            "phone_offset_ms_mean": offset_ms,
            "phone_offset_ms_std": float(est.time_offset_ms.std),
            "phone_rtt_ms_mean": float(est.roundtrip_duration_ms.mean),
            "phone_rtt_ms_std": float(est.roundtrip_duration_ms.std),
            "measurements": int(n),
        }

    def _neon_time_echo_sample(self) -> Optional[Dict]:
        """Periodic Pupil time-echo (≥2 samples — Pupil Estimate needs stdev)."""
        if not self.device:
            return None
        try:
            # n=1 → Pupil logger.exception(StatisticsError) spam; use 2+.
            est = self.device.estimate_time_offset(
                number_of_measurements=2,
                sleep_between_measurements_seconds=None,
            )
        except Exception as e:
            print(f"[pcHubSync] neon echo error: {e}")
            return None
        if est is None:
            return None
        return self._neon_time_echo_from_est(est, n=2)

    def _neon_time_echo_burst(self) -> Optional[Dict]:
        """Initial Neon lock (faster than Quest burst)."""
        if not self.device:
            return None
        try:
            est = self.device.estimate_time_offset(
                number_of_measurements=20,
                sleep_between_measurements_seconds=None,
            )
        except Exception as e:
            print(f"[pcHubSync] neon burst error: {e}")
            return None
        return self._neon_time_echo_from_est(est, n=20)

    def _hub_sync_ready(self) -> bool:
        return bool(self.device and self.tcp_thread and self.tcp_thread.conn)

    def _start_hub_sync(self, *, auto: bool = False) -> bool:
        if self._time_echo_monitor is not None:
            try:
                if self._time_echo_monitor.is_alive():
                    return True
            except TypeError:
                pass
            self._time_echo_monitor = None

        if not self._hub_sync_ready():
            if not auto:
                QMessageBox.information(
                    self,
                    "PC hub sync",
                    "Connect Neon and Quest (TCP) first.",
                )
            return False

        self.ensure_dirs()
        period_s = float(self.sync_period_spin.value())
        self._time_echo_monitor = PcHubSyncMonitor(
            self.tcp_thread,
            participant_dir_fn=lambda: self.participant_dir,
            period_s=period_s,
            burst_n=100,
            rolling_n=100,
            echo_timeout_s=1.0,
            neon_sample_fn=self._neon_time_echo_sample,
            neon_burst_fn=self._neon_time_echo_burst,
            quest_echo_cb=self._log_quest_echo,
            neon_echo_cb=self._log_neon_echo,
            status_cb=self.status_sync,
        )
        self._time_echo_monitor.start()
        self.btn_time_echo.setText("Stop PC hub sync")
        tag = "auto-started" if auto else "started"
        self.status_sync(f"{tag} (Quest+Neon every {period_s:.0f}s)")
        return True

    def _maybe_auto_start_hub_sync(self):
        if self._hub_sync_user_stopped:
            return
        if self._time_echo_monitor is not None:
            try:
                if self._time_echo_monitor.is_alive():
                    return
            except TypeError:
                pass
        self._start_hub_sync(auto=True)

    def on_toggle_time_echo(self):
        mon = self._time_echo_monitor
        if mon is not None:
            try:
                alive = mon.is_alive()
            except TypeError:
                alive = False
            if alive:
                mon.stop()
                mon.join(timeout=2.0)
                self._time_echo_monitor = None
                self._hub_sync_user_stopped = True
                self.btn_time_echo.setText("Start PC hub sync")
                self.status_sync("stopped")
                return
            self._time_echo_monitor = None

        self._hub_sync_user_stopped = False
        if self._start_hub_sync(auto=False):
            return

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
            return
        if mtype == "sessionHello":
            payload = msg.get("payload") or {}
            self._session_hello.emit(dict(payload))
            return

    def _on_session_hello(self, payload: dict):
        package = str(payload.get("package", ""))
        scene = str(payload.get("scene", ""))
        pending = self.session.on_session_hello(package, scene)
        print(f"[sessionHello] package={package} scene={scene} pending={pending or '-'}")
        if self.session.app in ("practice", "main_study"):
            self._want_study_gaze = True
        elif self.session.app == "openeye":
            self._want_study_gaze = False
        self._refresh_session_ui()
        QTimer.singleShot(200, self._auto_resume_study_gaze)

    def _auto_resume_study_gaze(self):
        if not self._want_study_gaze:
            return
        if not (self.tcp_thread and self.tcp_thread.conn):
            return
        if self.session.gaze == GazeMode.STUDY and self._gaze_tx_thread is not None and self._gaze_tx_thread.is_alive():
            return
        # Infer practice/main if still unknown (old Practice APK, no sessionHello).
        if self.session.app in ("none", "unknown") and self.session.handoff_pending:
            self.session.on_session_hello(self.session.handoff_pending, "connected")
        ok = self._set_gaze_mode(GazeMode.STUDY, quiet=True)
        print(f"[gaze] auto-resume STUDY: {'ok' if ok else 'FAILED'} app={self.session.app}")
        self._refresh_session_ui()

    def _on_main_study_done(self, payload: dict):
        cond = payload.get("condition", "?")
        ok = payload.get("ok", True)
        sub = payload.get("sub_num", "?")
        subsub = payload.get("subsub_num", "?")
        reps = payload.get("reps", "?")
        if payload.get("stopped"):
            reason = "stopped"
        else:
            reason = "done" if ok else "failed"
        self._reset_main_study_pc_idle(reason=reason)
        self.ms_status.setText(
            f"Status: {reason} — sub {sub}-{subsub} {cond} ×{reps}; IDLE again"
        )
        print(f"[mainStudyDone] {payload}")
        self._want_study_gaze = True
        self._auto_resume_study_gaze()
        self._refresh_session_ui()

    def _on_sync_pulse_record(self, record: dict):
        # Legacy Quest→Neon one-way pulses: log only. PC hub sync.json comes from
        # PcHubSyncMonitor (Quest↔PC + Neon↔PC time-echo), not quest_sync events.
        self._ensure_sync_pulse_logger()
        self._sync_pulse_logger.log(record)
        print(
            f"[syncPulse] seq={record['seq']} quest_ms={record['quest_sent_unix_ms']} "
            f"pc_ms={record['pc_recv_unix_ms']} (logged only; use PC hub sync)"
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
        if self.session.gaze != GazeMode.OFF:
            self._want_study_gaze = False
            self._set_gaze_mode(GazeMode.OFF)
            return

        if self.session.app in ("practice", "main_study") or self._want_study_gaze:
            self._set_gaze_mode(GazeMode.STUDY)
        else:
            self._set_gaze_mode(GazeMode.OPENEYE_VIZ)

    def on_launch_practice(self):
        self.go_to(
            PKG_PRACTICE,
            title="Start Practice",
            detail=(
                "Launch PracticeTask on the Quest?\n"
                f"Trial: {self.session.trial_id}\n"
                f"Package: {PKG_PRACTICE}"
            ),
        )

    def on_launch_study(self):
        self.go_to(
            PKG_MAIN_STUDY,
            title="Start Main Study",
            detail=(
                "Launch MainStudy (PC-commanded) on the Quest?\n"
                f"Trial: {self.session.trial_id}\n"
                f"Package: {PKG_MAIN_STUDY}"
            ),
        )

    def on_recalibrate(self):
        self._want_study_gaze = False
        self.go_to(
            PKG_OPENEYE,
            title="Recalibrate",
            detail=(
                "Launch OpenEye calibration on the Quest?\n"
                f"Trial: {self.session.trial_id}\n"
                f"Package: {PKG_OPENEYE}"
            ),
        )

    def on_main_study_start(self):
        if not (self.tcp_thread and self.tcp_thread.conn):
            QMessageBox.information(
                self, "Main Study",
                "Quest MainStudy must be connected on TCP first (IDLE screen).",
            )
            return
        if self._ms_running or self.session.main == "running":
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
            f"Trial: {self.session.trial_id}\n"
            f"Participant {sub_num}-{subsub_num}\n"
            f"Condition: {condition}\n"
            f"Fitts reps: {reps}\n"
            f"Each rep duration: {duration_sec // 60} min ({duration_sec}s)\n\n"
            f"Quest returns to IDLE when finished.",
        )
        if confirm != QMessageBox.Yes:
            return

        if condition.startswith("Eye"):
            if not self._set_gaze_mode(GazeMode.STUDY):
                return

        self.tcp_thread.send_main_study_start(
            sub_num=sub_num,
            subsub_num=subsub_num,
            condition=condition,
            reps=reps,
            duration_sec=duration_sec,
        )
        self._ms_running = True
        self.session.on_main_study_started()
        self.btn_ms_start.setEnabled(False)
        self.btn_ms_stop.setEnabled(True)
        self.ms_status.setText(
            f"Status: running {condition} ×{reps} ({duration_sec // 60} min each) "
            f"for {sub_num}-{subsub_num}…"
        )
        self._refresh_session_ui()

    def on_main_study_stop(self):
        """Abort current Main Study condition: reset PC to IDLE and tell Quest to stop."""
        if self.tcp_thread and self.tcp_thread.conn:
            try:
                self.tcp_thread.send_main_study_stop()
            except Exception as e:
                print(f"[mainStudyStop] send failed: {e}")
        else:
            print("[mainStudyStop] no TCP — resetting PC state only")

        self._reset_main_study_pc_idle(reason="stopped")

    def _reset_main_study_pc_idle(self, *, reason: str = "idle"):
        self._ms_running = False
        self.session.on_main_study_done()
        self.btn_ms_start.setEnabled(True)
        self.btn_ms_stop.setEnabled(True)
        self.ms_status.setText(
            f"Status: {reason} — IDLE (ready for next Start condition)"
        )
        print(f"[mainStudy] PC → IDLE ({reason})")
        self._refresh_session_ui()

    def load_models(self, quiet: bool = False):
        self.ensure_dirs()
        model_dir = os.path.join(self.participant_dir, "models")
        model_dir = os.path.abspath(model_dir)
        if not os.path.isdir(model_dir):
            msg = f"Models folder not found: {model_dir}"
            print(f"[models] {msg}")
            if not quiet:
                QMessageBox.warning(self, "Models", msg)
            return False
        self.models = load_models(model_dir)
        if not self.models or "ridge_biquadratic" not in self.models:
            msg = f"No ridge_biquadratic model in {model_dir}"
            print(f"[models] {msg}")
            if not quiet:
                QMessageBox.warning(self, "Models", msg)
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
    def _calib_min_dwell_s(self) -> float:
        return float(CFG.mapping.min_step_dwell_s)

    def _arm_calib_dwell(self):
        """Require min dwell on the current dot before Next is allowed."""
        dwell = self._calib_min_dwell_s()
        self._calib_dot_shown_at = time.perf_counter()
        self.btn_next_step.setEnabled(False)
        self.step_label.setText(
            f"Step: {self.state.step + 1} / 25 — fixate ≥{dwell:.0f}s, then Next"
        )
        self._calib_dwell_timer.stop()
        self._calib_dwell_timer.start(int(max(0.05, dwell) * 1000))

    def _on_calib_dwell_ready(self):
        if not self.state.recording or self.state.ended:
            return
        self.btn_next_step.setEnabled(True)
        self.step_label.setText(
            f"Step: {self.state.step + 1} / 25 — fixate, then Next (G/H/I/J)"
        )

    def trigger_step(self):
        if self.state.ended:
            return
        if not self.state.recording:
            QMessageBox.information(self, "Info", "Press 'S' to start recording first.")
            return

        dwell = self._calib_min_dwell_s()
        elapsed = time.perf_counter() - self._calib_dot_shown_at
        if elapsed < dwell:
            remain = dwell - elapsed
            self.step_label.setText(
                f"Step: {self.state.step + 1} / 25 — wait {remain:.1f}s more before Next"
            )
            return

        prev_events = self._gaze_event_count()
        # Log gaze for the dot currently shown (step is 0-based dot index).
        self.state.set_event()

        if self.state.step < 24:
            self.state.step += 1
            if self.tcp_thread and self.tcp_thread.conn:
                self.tcp_thread.send_step(self.state.step)
            self._arm_calib_dwell()
        else:
            self._calib_dwell_timer.stop()
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
                self.session.on_calib_finished()
                self.load_models()
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
            self._refresh_session_ui()

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

    # ---- URP2026 recorder (phone PC-bridge :8765) ----
    def _save_urp_settings(self):
        self._settings.setValue("urp2026/host", self.urp_host.text().strip())
        self._settings.setValue("urp2026/port", int(self.urp_port.value()))

    def _set_urp_buttons_enabled(self, enabled: bool):
        for b in self._urp_buttons:
            b.setEnabled(enabled)

    def _urp_send(self, command: str):
        host = self.urp_host.text().strip()
        if not host:
            QMessageBox.information(
                self, "URP2026",
                "Enter the phone IP first (URP2026 → Start PC bridge (:8765)).",
            )
            return
        port = int(self.urp_port.value())
        timeout = float(CFG.urp2026.timeout_s)
        self._save_urp_settings()
        self._set_urp_buttons_enabled(False)
        self.urp_status.setText(f"URP2026 [{command}] …")

        def worker():
            try:
                code, body = urp_send_command(host, command, port=port, timeout=timeout)
                self._urp_result.emit(command, int(code), body, code < 400)
            except Urp2026Error as e:
                self._urp_result.emit(command, 0, str(e), False)

        threading.Thread(target=worker, daemon=True).start()

    def _on_urp_result(self, command: str, code: int, body: str, ok: bool):
        self._set_urp_buttons_enabled(True)
        head = "OK" if ok else "ERR"
        code_txt = f" HTTP {code}" if code else ""
        lines = [ln for ln in (body or "").splitlines() if ln.strip()]
        first = lines[0] if lines else ""
        self.urp_status.setText(f"URP2026 [{command}] {head}{code_txt}: {first}")
        print(f"[urp2026] {command} ok={ok} code={code}\n{body}")

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