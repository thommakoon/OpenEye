"""Trial-scoped session + gaze state machine for OpenEye PC GUI."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from enum import Enum


PKG_OPENEYE = "org.MixedRealityToolkit.MRTK3Sample"
PKG_PRACTICE = "com.PracticeMG.MRstressPRACTICE"
PKG_MAIN_STUDY = "com.PracticeMG.MRstress"

PKG_TO_APP = {
    PKG_OPENEYE: "openeye",
    PKG_PRACTICE: "practice",
    PKG_MAIN_STUDY: "main_study",
}


class GazeMode(str, Enum):
    """PC → Quest mapped-gaze TCP stream."""

    OFF = "off"
    OPENEYE_VIZ = "openeye_viz"  # Visualize on OpenEye calib app
    STUDY = "study"  # Stream to Practice / MainStudy (no calib Dot)


def trial_id_from_num(p_num: int) -> str:
    return f"t{p_num:02d}"


def model_dir_for_trial(trial_id: str) -> str:
    return os.path.join(trial_id, "models")


def trial_has_model(trial_id: str) -> bool:
    model_dir = model_dir_for_trial(trial_id)
    if not os.path.isdir(model_dir):
        return False
    return os.path.isfile(os.path.join(model_dir, "model_ridge_biquadratic.json"))


@dataclass
class TrialSession:
    """Per-trial session state owned by the PC GUI (source of truth)."""

    trial_id: str = "t00"
    model_ready: bool = False

    app: str = "none"  # none | unknown | openeye | practice | main_study
    tcp_connected: bool = False
    tcp_peer_package: str = ""

    openeye: str = "no_model"  # no_model | calibrating | ready
    main: str = "idle"  # idle | running

    gaze: GazeMode = GazeMode.OFF
    handoff_pending: str = ""  # target package; kept until sessionHello

    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def gaze_to_quest(self) -> bool:
        return self.gaze != GazeMode.OFF

    def set_trial(self, trial_id: str) -> None:
        with self.lock:
            self.trial_id = trial_id
            self.model_ready = trial_has_model(trial_id)
            if not self.model_ready:
                self.openeye = "no_model"
            elif self.openeye == "no_model":
                self.openeye = "ready"

    def refresh_model(self) -> bool:
        with self.lock:
            self.model_ready = trial_has_model(self.trial_id)
            if self.model_ready and self.openeye == "no_model":
                self.openeye = "ready"
            return self.model_ready

    def on_tcp_connected(self) -> None:
        """Socket accepted — do NOT clear handoff_pending (sessionHello does that)."""
        with self.lock:
            self.tcp_connected = True
            self.app = "unknown"
            self.tcp_peer_package = ""

    def on_tcp_disconnected(self) -> None:
        with self.lock:
            self.tcp_connected = False
            self.app = "none"
            self.tcp_peer_package = ""
            self.main = "idle"
            # Keep handoff_pending so we still auto-resume gaze after the next app connects.

    def on_session_hello(self, package: str, scene: str = "") -> str:
        """Apply hello; returns previous handoff_pending (for auto-resume decisions)."""
        with self.lock:
            pending = self.handoff_pending
            self.tcp_peer_package = package or ""
            self.app = PKG_TO_APP.get(package, "unknown")
            scene_u = (scene or "").upper()
            if self.app == "main_study":
                if scene_u in ("TRIAL", "BEFORE_TRIAL", "AFTER_TRIAL", "PREP", "BREAK"):
                    self.main = "running"
                else:
                    self.main = "idle"
            elif self.app == "openeye":
                if scene_u in ("CALIBRATING", "CALIB"):
                    self.openeye = "calibrating"
                elif self.model_ready:
                    self.openeye = "ready"
            self.handoff_pending = ""
            return pending

    def on_calib_started(self) -> None:
        with self.lock:
            self.openeye = "calibrating"

    def on_calib_finished(self) -> None:
        with self.lock:
            self.model_ready = True
            self.openeye = "ready"

    def on_main_study_started(self) -> None:
        with self.lock:
            self.main = "running"

    def on_main_study_done(self) -> None:
        with self.lock:
            self.main = "idle"

    def set_gaze(self, mode: GazeMode) -> None:
        with self.lock:
            self.gaze = mode

    def note_handoff(self, target_package: str) -> None:
        with self.lock:
            self.handoff_pending = target_package or ""
            self.gaze = GazeMode.OFF

    def wants_study_gaze_after_connect(self) -> bool:
        with self.lock:
            return self.model_ready and self.app in ("practice", "main_study")

    def can_handoff_to(self, target_package: str) -> tuple[bool, str]:
        with self.lock:
            if not self.tcp_connected:
                return False, "No Quest app connected on TCP. Start TCP server and open an app on Quest."

            if self.main == "running" and target_package != PKG_OPENEYE:
                return (
                    False,
                    "Main Study condition is running. Wait for mainStudyDone or launch OpenEye to recalibrate.",
                )

            if target_package in (PKG_PRACTICE, PKG_MAIN_STUDY) and not self.model_ready:
                return (
                    False,
                    f"Trial {self.trial_id}: no calibration model yet. Complete OpenEye calibration first.",
                )

            return True, ""

    def format_line(self) -> str:
        with self.lock:
            parts = [f"Trial: {self.trial_id}"]
            parts.append(f"model: {'ready' if self.model_ready else 'none'}")
            if self.tcp_connected:
                app = self.app if self.app != "unknown" else "quest(?)"
                parts.append(f"app: {app}")
                if self.app == "openeye":
                    parts.append(f"openeye: {self.openeye}")
                if self.app == "main_study":
                    parts.append(f"main: {self.main}")
            else:
                parts.append("app: none")
            parts.append(f"gaze: {self.gaze.value}")
            if self.handoff_pending:
                parts.append(f"handoff→{PKG_TO_APP.get(self.handoff_pending, '?')}")
            return " | ".join(parts)
