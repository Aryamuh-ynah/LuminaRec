from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QGuiApplication, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSizePolicy, QSpacerItem, QVBoxLayout, QWidget
)

from recorder import RecorderConfig, RecorderController
from utils import WindowInfo, default_output_path, format_duration, human_size, is_wayland, list_x11_windows


DARK_STYLE = """
QWidget { background:#15171a; color:#e9edf1; font-size:14px; }
QMainWindow { background:#111316; }
QFrame#card { background:#1c2025; border:1px solid #2b3138; border-radius:14px; }
QComboBox, QPushButton, QLineEdit, QListWidget { background:#252a31; border:1px solid #343b44; border-radius:9px; padding:8px; }
QComboBox:hover, QPushButton:hover { border-color:#6a7cff; }
QPushButton#startButton { background:#5b6cff; color:white; border:0; border-radius:16px; font-size:22px; font-weight:700; padding:18px; }
QPushButton#startButton[recording="true"] { background:#d84a57; }
QPushButton:disabled { color:#747b84; background:#20242a; }
QLabel#timer { font-size:34px; font-weight:700; }
QLabel#muted { color:#9aa4af; }
QCheckBox { spacing:8px; }
"""

LIGHT_STYLE = """
QWidget { background:#f5f6f8; color:#1c232b; font-size:14px; }
QMainWindow { background:#eef0f3; }
QFrame#card { background:white; border:1px solid #d7dce2; border-radius:14px; }
QComboBox, QPushButton, QLineEdit, QListWidget { background:white; border:1px solid #cfd5dc; border-radius:9px; padding:8px; }
QPushButton#startButton { background:#5366ee; color:white; border:0; border-radius:16px; font-size:22px; font-weight:700; padding:18px; }
QPushButton#startButton[recording="true"] { background:#d84a57; }
QLabel#timer { font-size:34px; font-weight:700; }
QLabel#muted { color:#67717d; }
"""


class Bridge(QObject):
    preview = Signal(object)
    state = Signal(str)
    error = Signal(str)
    finished = Signal(object)
    hotkey = Signal()


class RegionSelector(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)
        self.origin: QPoint | None = None
        self.current: QPoint | None = None
        virtual = QGuiApplication.primaryScreen().virtualGeometry()
        self.setGeometry(virtual)

    def selection_global(self) -> dict[str, int] | None:
        if self.origin is None or self.current is None:
            return None
        rect = QRect(self.origin, self.current).normalized()
        if rect.width() < 4 or rect.height() < 4:
            return None
        top_left = self.mapToGlobal(rect.topLeft())
        return {"left": top_left.x(), "top": top_left.y(), "width": rect.width(), "height": rect.height()}

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.origin = event.position().toPoint()
            self.current = self.origin
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self.origin is not None:
            self.current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.origin is not None:
            self.current = event.position().toPoint()
            if self.selection_global():
                self.accept()
            else:
                self.reject()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 105))
        pos = self.mapFromGlobal(QGuiApplication.primaryScreen().geometry().center())
        if self.current is not None:
            pos = self.current
        painter.setPen(QPen(QColor(255, 255, 255, 100), 1))
        painter.drawLine(0, pos.y(), self.width(), pos.y())
        painter.drawLine(pos.x(), 0, pos.x(), self.height())
        if self.origin is not None and self.current is not None:
            rect = QRect(self.origin, self.current).normalized()
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(rect, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor(97, 116, 255), 2))
            painter.drawRect(rect)
            label = f"{rect.width()} × {rect.height()}"
            painter.fillRect(QRect(rect.left(), max(0, rect.top() - 30), 110, 26), QColor(20, 23, 28, 220))
            painter.setPen(Qt.white)
            painter.drawText(rect.left() + 8, max(18, rect.top() - 11), label)


class WindowPicker(QDialog):
    def __init__(self, windows: list[WindowInfo], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose a window")
        self.resize(620, 420)
        self.list = QListWidget()
        for win in windows:
            item = QListWidgetItem(f"{win.title}   ({win.width}×{win.height})")
            item.setData(Qt.UserRole, win)
            self.list.addItem(item)
        choose = QPushButton("Record selected window")
        cancel = QPushButton("Cancel")
        choose.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        row = QHBoxLayout()
        row.addStretch(1); row.addWidget(cancel); row.addWidget(choose)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Open windows")); layout.addWidget(self.list); layout.addLayout(row)

    def selected_window(self) -> WindowInfo | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Luma Recorder")
        self.resize(900, 680)
        self.setMinimumSize(720, 560)
        self.recorder = RecorderController()
        self.bridge = Bridge()
        self.bridge.preview.connect(self._show_preview)
        self.bridge.state.connect(self._on_state)
        self.bridge.error.connect(self._on_error)
        self.bridge.finished.connect(self._on_finished)
        self.bridge.hotkey.connect(self._toggle_recording)
        self.region: dict[str, int] | None = None
        self.window_info: WindowInfo | None = None
        self._dark = True
        self._build_ui()
        self._apply_theme()
        self._install_hotkey()
        self.stats_timer = QTimer(self)
        self.stats_timer.timeout.connect(self._update_stats)
        self.stats_timer.start(250)

    def _build_ui(self) -> None:
        root = QWidget(); self.setCentralWidget(root)
        outer = QVBoxLayout(root); outer.setContentsMargins(24, 24, 24, 24); outer.setSpacing(16)

        top = QHBoxLayout()
        title_col = QVBoxLayout()
        title = QLabel("Luma Recorder"); title.setStyleSheet("font-size:26px;font-weight:800;")
        sub = QLabel("Fast X11 capture • secure Wayland portal capture"); sub.setObjectName("muted")
        title_col.addWidget(title); title_col.addWidget(sub)
        top.addLayout(title_col); top.addStretch(1)
        self.theme_btn = QPushButton("Light theme")
        self.theme_btn.clicked.connect(self._toggle_theme)
        top.addWidget(self.theme_btn)
        outer.addLayout(top)

        card = QFrame(); card.setObjectName("card")
        form = QFormLayout(card); form.setContentsMargins(20,20,20,20); form.setHorizontalSpacing(18); form.setVerticalSpacing(14)
        self.mode = QComboBox(); self.mode.addItem("Full Screen", "full"); self.mode.addItem("Region", "region"); self.mode.addItem("Window", "window")
        self.fps = QComboBox(); [self.fps.addItem(str(x), x) for x in (15,24,30,60)]; self.fps.setCurrentText("30")
        self.format = QComboBox(); self.format.addItem("MP4 — H.264", "mp4"); self.format.addItem("WebM — VP9", "webm")
        self.audio = QComboBox(); self.audio.addItem("None", "none"); self.audio.addItem("Microphone", "mic"); self.audio.addItem("System audio", "system"); self.audio.addItem("Mic + System", "both")
        self.preview_toggle = QCheckBox("Show live preview"); self.preview_toggle.setChecked(True)
        form.addRow("Capture mode", self.mode); form.addRow("Frames / second", self.fps); form.addRow("Output format", self.format); form.addRow("Audio", self.audio); form.addRow("Preview", self.preview_toggle)
        outer.addWidget(card)

        output_card = QFrame(); output_card.setObjectName("card")
        out_layout = QHBoxLayout(output_card); out_layout.setContentsMargins(20,16,20,16)
        self.output_label = QLabel(str(default_output_path("mp4"))); self.output_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.output_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        browse = QPushButton("Choose…"); browse.clicked.connect(self._choose_output)
        out_layout.addWidget(QLabel("Save to")); out_layout.addWidget(self.output_label, 1); out_layout.addWidget(browse)
        outer.addWidget(output_card)
        self.format.currentIndexChanged.connect(self._format_changed)

        stats_card = QFrame(); stats_card.setObjectName("card")
        stats = QHBoxLayout(stats_card); stats.setContentsMargins(20,18,20,18)
        self.timer_label = QLabel("00:00:00"); self.timer_label.setObjectName("timer")
        status_col = QVBoxLayout(); self.status_label = QLabel("Ready"); self.status_label.setStyleSheet("font-weight:700;")
        self.detail_label = QLabel("0.0 FPS • 0 B"); self.detail_label.setObjectName("muted")
        status_col.addWidget(self.status_label); status_col.addWidget(self.detail_label)
        stats.addWidget(self.timer_label); stats.addSpacing(28); stats.addLayout(status_col); stats.addStretch(1)
        outer.addWidget(stats_card)

        self.preview = QLabel("Preview")
        self.preview.setAlignment(Qt.AlignCenter); self.preview.setMinimumHeight(150); self.preview.setMaximumHeight(240)
        self.preview.setStyleSheet("background:#08090b;border-radius:12px;color:#68727e;")
        outer.addWidget(self.preview, 1)

        controls = QHBoxLayout()
        self.pause_btn = QPushButton("Pause"); self.pause_btn.setEnabled(False); self.pause_btn.clicked.connect(self._pause_resume)
        self.start_btn = QPushButton("Start Recording"); self.start_btn.setObjectName("startButton"); self.start_btn.clicked.connect(self._toggle_recording)
        controls.addWidget(self.pause_btn); controls.addWidget(self.start_btn, 1)
        outer.addLayout(controls)

        hint = QLabel("Ctrl+Shift+R: start/stop • Esc: cancel region selection")
        hint.setAlignment(Qt.AlignCenter); hint.setObjectName("muted"); outer.addWidget(hint)

    def _apply_theme(self) -> None:
        QApplication.instance().setStyleSheet(DARK_STYLE if self._dark else LIGHT_STYLE)
        self.theme_btn.setText("Light theme" if self._dark else "Dark theme")

    def _toggle_theme(self) -> None:
        self._dark = not self._dark; self._apply_theme()

    def _install_hotkey(self) -> None:
        shortcut = QShortcut(QKeySequence("Ctrl+Shift+R"), self)
        shortcut.setContext(Qt.ApplicationShortcut)
        shortcut.activated.connect(self._toggle_recording)
        self._qt_shortcut = shortcut
        self._global_hotkeys = None
        if not is_wayland():
            try:
                from pynput import keyboard
                self._global_hotkeys = keyboard.GlobalHotKeys({"<ctrl>+<shift>+r": lambda: self.bridge.hotkey.emit()})
                self._global_hotkeys.start()
            except Exception:
                self._global_hotkeys = None

    def _format_changed(self) -> None:
        fmt = self.format.currentData()
        path = Path(self.output_label.text())
        self.output_label.setText(str(path.with_suffix("." + fmt)))

    def _choose_output(self) -> None:
        fmt = self.format.currentData()
        filt = "MP4 video (*.mp4)" if fmt == "mp4" else "WebM video (*.webm)"
        path, _ = QFileDialog.getSaveFileName(self, "Save recording", self.output_label.text(), filt)
        if path:
            p = Path(path)
            if p.suffix.lower() != "." + fmt:
                p = p.with_suffix("." + fmt)
            self.output_label.setText(str(p))

    def _pick_region(self) -> bool:
        selector = RegionSelector(self)
        if selector.exec() != QDialog.Accepted:
            return False
        self.region = selector.selection_global()
        return self.region is not None

    def _pick_window(self) -> bool:
        if is_wayland():
            self.window_info = None
            return True
        try:
            windows = [w for w in list_x11_windows() if "Luma Recorder" not in w.title]
        except Exception as exc:
            QMessageBox.critical(self, "Window selection unavailable", str(exc)); return False
        if not windows:
            QMessageBox.warning(self, "No windows", "No recordable X11 windows were found."); return False
        dlg = WindowPicker(windows, self)
        if dlg.exec() != QDialog.Accepted:
            return False
        self.window_info = dlg.selected_window()
        return self.window_info is not None

    def _toggle_recording(self) -> None:
        state = self.recorder.stats().state
        if state in ("starting", "recording", "paused"):
            self.recorder.stop(); return
        if state == "finalizing":
            return
        mode = self.mode.currentData()
        self.region = None; self.window_info = None
        if mode == "region" and not self._pick_region():
            return
        if mode == "window" and not self._pick_window():
            return
        region = self.region
        if mode == "window" and self.window_info is not None:
            region = self.window_info.region
        config = RecorderConfig(
            mode=mode,
            fps=int(self.fps.currentData()),
            format=self.format.currentData(),
            audio=self.audio.currentData(),
            output=Path(self.output_label.text()).expanduser(),
            preview=self.preview_toggle.isChecked(),
            region=region,
        )
        try:
            self.recorder.start(
                config,
                preview_cb=lambda frame: self.bridge.preview.emit(frame.copy()),
                state_cb=self.bridge.state.emit,
                error_cb=self.bridge.error.emit,
                finished_cb=self.bridge.finished.emit,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Could not start recording", str(exc))

    def _pause_resume(self) -> None:
        state = self.recorder.stats().state
        if state == "recording": self.recorder.pause()
        elif state == "paused": self.recorder.resume()

    def _on_state(self, state: str) -> None:
        labels = {"starting":"Waiting for capture permission…" if is_wayland() else "Starting…", "recording":"Recording", "paused":"Paused", "finalizing":"Finalizing…", "finished":"Saved", "error":"Error"}
        self.status_label.setText(labels.get(state, state.title()))
        recording = state in ("starting", "recording", "paused", "finalizing")
        self.start_btn.setText("Stop Recording" if recording and state != "finalizing" else ("Finalizing…" if state == "finalizing" else "Start Recording"))
        self.start_btn.setProperty("recording", recording and state != "finalizing")
        self.start_btn.style().unpolish(self.start_btn); self.start_btn.style().polish(self.start_btn)
        self.start_btn.setEnabled(state != "finalizing")
        self.pause_btn.setEnabled(state in ("recording", "paused"))
        self.pause_btn.setText("Resume" if state == "paused" else "Pause")
        for widget in (self.mode, self.fps, self.format, self.audio):
            widget.setEnabled(not recording)

    def _on_error(self, message: str) -> None:
        QMessageBox.critical(self, "Recording error", message)
        self._on_state("error")

    def _on_finished(self, path: Path) -> None:
        self.status_label.setText(f"Saved to {path}")
        self._on_state("finished")

    def _update_stats(self) -> None:
        stats = self.recorder.stats()
        self.timer_label.setText(format_duration(stats.elapsed))
        self.detail_label.setText(f"{stats.current_fps:.1f} FPS • {human_size(stats.bytes_written)} estimated")

    def _show_preview(self, frame: np.ndarray) -> None:
        if not self.preview_toggle.isChecked() or frame.size == 0:
            return
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
        h, w, _ = rgb.shape
        image = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        pix = QPixmap.fromImage(image).scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview.setPixmap(pix)

    def closeEvent(self, event) -> None:
        state = self.recorder.stats().state
        if state in ("starting", "recording", "paused", "finalizing"):
            self.recorder.stop()
        try:
            if self._global_hotkeys:
                self._global_hotkeys.stop()
        except Exception:
            pass
        event.accept()
