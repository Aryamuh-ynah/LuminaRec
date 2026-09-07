from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QGuiApplication, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from portal import PortalError, ScreenCastPortal
from recorder import RecorderConfig, RecorderController
from utils import WindowInfo, default_output_path, format_duration, human_size, is_wayland, list_x11_windows


DARK_STYLE = """
QWidget {
    color: #e9edf1;
    font-size: 14px;
}

QMainWindow,
QWidget#rootWidget {
    background: #111316;
}

QFrame#card {
    background: #1c2025;
    border: 1px solid #2b3138;
    border-radius: 14px;
}

/* Prevent ugly dark rectangles behind text */
QLabel,
QCheckBox {
    background: transparent;
    border: none;
}

QLabel#muted {
    color: #9aa4af;
}

QLabel#timer {
    font-size: 32px;
    font-weight: 700;
}

QComboBox,
QLineEdit,
QListWidget {
    background: #252a31;
    color: #e9edf1;
    border: 1px solid #343b44;
    border-radius: 9px;
    padding: 7px 10px;
    selection-background-color: #5b6cff;
}

QComboBox:hover,
QLineEdit:hover {
    border-color: #4d5866;
}

QComboBox:focus,
QLineEdit:focus {
    border-color: #6a7cff;
}

QComboBox::drop-down {
    width: 28px;
    border: none;
}

QComboBox QAbstractItemView {
    background: #252a31;
    color: #e9edf1;
    border: 1px solid #343b44;
    selection-background-color: #5b6cff;
    outline: none;
}

QLineEdit:read-only {
    background: #16191d;
    color: #d5dbe2;
}

QPushButton {
    background: #252a31;
    color: #e9edf1;
    border: 1px solid #343b44;
    border-radius: 9px;
    padding: 8px 12px;
}

QPushButton:hover {
    background: #2b3038;
    border-color: #6a7cff;
}

QPushButton:pressed {
    background: #20242a;
}

QPushButton:disabled {
    color: #747b84;
    background: #20242a;
    border-color: #292e34;
}

QPushButton#startButton {
    background: #5b6cff;
    color: white;
    border: none;
    border-radius: 13px;
    font-size: 18px;
    font-weight: 700;
    padding: 12px 18px;
}

QPushButton#startButton:hover {
    background: #6878ff;
}

QPushButton#startButton[recording="true"] {
    background: #d84a57;
}

QPushButton#startButton[recording="true"]:hover {
    background: #e15360;
}

QCheckBox {
    spacing: 8px;
}

QCheckBox::indicator {
    width: 17px;
    height: 17px;
}
"""

LIGHT_STYLE = """
QWidget {
    color: #1c232b;
    font-size: 14px;
}

QMainWindow,
QWidget#rootWidget {
    background: #eef0f3;
}

QFrame#card {
    background: #ffffff;
    border: 1px solid #d7dce2;
    border-radius: 14px;
}

QLabel,
QCheckBox {
    background: transparent;
    border: none;
}

QLabel#muted {
    color: #67717d;
}

QLabel#timer {
    font-size: 32px;
    font-weight: 700;
}

QComboBox,
QLineEdit,
QListWidget {
    background: #ffffff;
    color: #1c232b;
    border: 1px solid #cfd5dc;
    border-radius: 9px;
    padding: 7px 10px;
    selection-background-color: #5366ee;
}

QComboBox:hover,
QLineEdit:hover {
    border-color: #9da7b3;
}

QComboBox:focus,
QLineEdit:focus {
    border-color: #5366ee;
}

QComboBox::drop-down {
    width: 28px;
    border: none;
}

QComboBox QAbstractItemView {
    background: white;
    color: #1c232b;
    border: 1px solid #cfd5dc;
    selection-background-color: #5366ee;
}

QLineEdit:read-only {
    background: #f7f8fa;
}

QPushButton {
    background: #ffffff;
    color: #1c232b;
    border: 1px solid #cfd5dc;
    border-radius: 9px;
    padding: 8px 12px;
}

QPushButton:hover {
    background: #f4f5f8;
    border-color: #5366ee;
}

QPushButton:disabled {
    color: #9da5ae;
    background: #eceef1;
}

QPushButton#startButton {
    background: #5366ee;
    color: white;
    border: none;
    border-radius: 13px;
    font-size: 18px;
    font-weight: 700;
    padding: 12px 18px;
}

QPushButton#startButton:hover {
    background: #6475f1;
}

QPushButton#startButton[recording="true"] {
    background: #d84a57;
}

QCheckBox {
    spacing: 8px;
}

QCheckBox::indicator {
    width: 17px;
    height: 17px;
}
"""


class Bridge(QObject):
    preview = Signal(object)
    state = Signal(str)
    error = Signal(str)
    finished = Signal(object)
    hotkey = Signal()


class RegionSelector(QDialog):
    def __init__(
        self,
        capture_geometry: QRect | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)

        self.origin: QPoint | None = None
        self.current: QPoint | None = None

        if capture_geometry is None:
            capture_geometry = (
                QGuiApplication.primaryScreen().virtualGeometry()
            )

        self.setGeometry(capture_geometry)

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
        self.resize(940, 700)
        self.setMinimumSize(720, 620)
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
        root = QWidget()
        root.setObjectName("rootWidget")
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(12)

        # ---------------------------------------------------------
        # Header
        # ---------------------------------------------------------
        top = QHBoxLayout()
        top.setSpacing(12)

        title_col = QVBoxLayout()
        title_col.setSpacing(4)

        title = QLabel("Luma Recorder")
        title.setStyleSheet(
            "font-size: 26px; font-weight: 800;"
        )

        sub = QLabel(
            "Fast X11 capture • secure Wayland portal capture"
        )
        sub.setObjectName("muted")

        title_col.addWidget(title)
        title_col.addWidget(sub)

        top.addLayout(title_col)
        top.addStretch(1)

        self.theme_btn = QPushButton("Light theme")
        self.theme_btn.setMinimumHeight(36)
        self.theme_btn.clicked.connect(self._toggle_theme)

        top.addWidget(self.theme_btn)

        outer.addLayout(top)

        # ---------------------------------------------------------
        # Recording settings
        # ---------------------------------------------------------
        settings_card = QFrame()
        settings_card.setObjectName("card")
        settings_card.setMinimumHeight(158)

        settings = QGridLayout(settings_card)
        settings.setContentsMargins(18, 16, 18, 16)
        settings.setHorizontalSpacing(14)
        settings.setVerticalSpacing(10)

        self.mode = QComboBox()
        self.mode.addItem("Full Screen", "full")
        self.mode.addItem("Region", "region")
        self.mode.addItem("Window", "window")

        self.fps = QComboBox()
        for value in (15, 24, 30, 60):
            self.fps.addItem(str(value), value)
        self.fps.setCurrentText("30")

        self.format = QComboBox()
        self.format.addItem("MP4 — H.264", "mp4")
        self.format.addItem("WebM — VP9", "webm")

        self.audio = QComboBox()
        self.audio.addItem("None", "none")
        self.audio.addItem("Microphone", "mic")
        self.audio.addItem("System audio", "system")
        self.audio.addItem("Mic + System", "both")

        # Prevent combo boxes from ever collapsing into thin lines.
        for combo in (
            self.mode,
            self.fps,
            self.format,
            self.audio,
        ):
            combo.setMinimumHeight(38)
            combo.setSizePolicy(
                QSizePolicy.Expanding,
                QSizePolicy.Fixed,
            )

        capture_label = QLabel("Capture mode")
        fps_label = QLabel("Frames / second")
        format_label = QLabel("Output format")
        audio_label = QLabel("Audio")

        settings.addWidget(capture_label, 0, 0)
        settings.addWidget(self.mode, 0, 1)

        settings.addWidget(fps_label, 0, 2)
        settings.addWidget(self.fps, 0, 3)

        settings.addWidget(format_label, 1, 0)
        settings.addWidget(self.format, 1, 1)

        settings.addWidget(audio_label, 1, 2)
        settings.addWidget(self.audio, 1, 3)

        self.preview_toggle = QCheckBox("Show live preview")
        self.preview_toggle.setChecked(True)

        settings.addWidget(
            self.preview_toggle,
            2,
            0,
            1,
            4,
        )

        settings.setColumnMinimumWidth(0, 100)
        settings.setColumnMinimumWidth(2, 105)

        settings.setColumnStretch(1, 1)
        settings.setColumnStretch(3, 1)

        outer.addWidget(settings_card)

        # ---------------------------------------------------------
        # Output path
        # ---------------------------------------------------------
        output_card = QFrame()
        output_card.setObjectName("card")

        output_layout = QHBoxLayout(output_card)
        output_layout.setContentsMargins(18, 13, 18, 13)
        output_layout.setSpacing(10)

        save_label = QLabel("Save to")
        save_label.setMinimumWidth(52)

        self.output_label = QLineEdit(
            str(default_output_path("mp4"))
        )
        self.output_label.setReadOnly(True)
        self.output_label.setMinimumHeight(38)
        self.output_label.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Fixed,
        )

        browse = QPushButton("Choose…")
        browse.setMinimumHeight(38)
        browse.setMinimumWidth(88)
        browse.clicked.connect(self._choose_output)

        output_layout.addWidget(save_label)
        output_layout.addWidget(self.output_label, 1)
        output_layout.addWidget(browse)

        outer.addWidget(output_card)

        self.format.currentIndexChanged.connect(
            self._format_changed
        )

        # ---------------------------------------------------------
        # Recording information
        # ---------------------------------------------------------
        stats_card = QFrame()
        stats_card.setObjectName("card")
        stats_card.setMinimumHeight(78)

        stats = QHBoxLayout(stats_card)
        stats.setContentsMargins(18, 14, 18, 14)
        stats.setSpacing(24)

        self.timer_label = QLabel("00:00:00")
        self.timer_label.setObjectName("timer")
        self.timer_label.setMinimumWidth(145)

        status_col = QVBoxLayout()
        status_col.setSpacing(3)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet(
            "font-weight: 700; font-size: 15px;"
        )

        self.detail_label = QLabel(
            "0.0 FPS • 0 B estimated"
        )
        self.detail_label.setObjectName("muted")

        status_col.addWidget(self.status_label)
        status_col.addWidget(self.detail_label)

        stats.addWidget(self.timer_label)
        stats.addLayout(status_col)
        stats.addStretch(1)

        outer.addWidget(stats_card)

        # ---------------------------------------------------------
        # Preview
        # ---------------------------------------------------------
        self.preview = QLabel("Live preview")
        self.preview.setAlignment(Qt.AlignCenter)

        self.preview.setMinimumHeight(110)
        self.preview.setMaximumHeight(220)

        self.preview.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding,
        )

        self.preview.setStyleSheet(
            """
            QLabel {
                background: #08090b;
                border: 1px solid #242930;
                border-radius: 12px;
                color: #68727e;
            }
            """
        )

        outer.addWidget(self.preview, 1)

        # ---------------------------------------------------------
        # Controls
        # ---------------------------------------------------------
        controls = QHBoxLayout()
        controls.setSpacing(10)

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setEnabled(False)
        self.pause_btn.setMinimumHeight(50)
        self.pause_btn.setMinimumWidth(130)
        self.pause_btn.clicked.connect(
            self._pause_resume
        )

        self.start_btn = QPushButton("Start Recording")
        self.start_btn.setObjectName("startButton")
        self.start_btn.setMinimumHeight(50)
        self.start_btn.clicked.connect(
            self._toggle_recording
        )

        controls.addWidget(self.pause_btn)
        controls.addWidget(self.start_btn, 1)

        outer.addLayout(controls)

        # ---------------------------------------------------------
        # Keyboard shortcut hint
        # ---------------------------------------------------------
        hint = QLabel(
            "Ctrl+Shift+R: start/stop • "
            "Esc: cancel region selection"
        )

        hint.setAlignment(Qt.AlignCenter)
        hint.setObjectName("muted")

        outer.addWidget(hint)


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

    def _pick_region(
        self,
        geometry: QRect | None = None,
    ) -> bool:
        # On Wayland don't parent the overlay when a specific monitor
        # has already been selected. A compositor may otherwise keep
        # the dialog on the parent's monitor.
        parent = None if geometry is not None and is_wayland() else self

        selector = RegionSelector(
            capture_geometry=geometry,
            parent=parent,
        )

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
            self.recorder.stop()
            return

        if state == "finalizing":
            return

        mode = self.mode.currentData()

        self.region = None
        self.window_info = None

        wayland_portal = None
        wayland_stream = None

        # ---------------------------------------------------------
        # REGION
        # ---------------------------------------------------------
        if mode == "region":
            if is_wayland():
                try:
                    # STEP 1:
                    # Ask Wayland/portal which monitor should be shared.
                    wayland_portal = ScreenCastPortal()

                    # type 1 = MONITOR
                    wayland_stream = wayland_portal.open(1)

                except Exception as exc:
                    if wayland_portal is not None:
                        wayland_portal.close()

                    QMessageBox.critical(
                        self,
                        "Screen selection failed",
                        str(exc),
                    )
                    return

                # STEP 2:
                # Now that we know which monitor was selected,
                # show the region selector only inside that monitor.
                monitor_geometry = QRect(
                    wayland_stream.x,
                    wayland_stream.y,
                    wayland_stream.width,
                    wayland_stream.height,
                )

                if not self._pick_region(monitor_geometry):
                    # User pressed Esc / cancelled the region selection.
                    try:
                        os.close(wayland_stream.fd)
                    except OSError:
                        pass

                    wayland_portal.close()
                    return

            else:
                # X11 keeps the existing behavior.
                if not self._pick_region():
                    return

        # ---------------------------------------------------------
        # WINDOW
        # ---------------------------------------------------------
        if mode == "window":
            if not self._pick_window():
                return

        # ---------------------------------------------------------
        # Resolve capture rectangle
        # ---------------------------------------------------------
        region = self.region

        if mode == "window" and self.window_info is not None:
            region = self.window_info.region

        # ---------------------------------------------------------
        # Build recorder configuration
        # ---------------------------------------------------------
        config = RecorderConfig(
            mode=mode,
            fps=int(self.fps.currentData()),
            format=self.format.currentData(),
            audio=self.audio.currentData(),
            output=Path(
                self.output_label.text()
            ).expanduser(),
            preview=self.preview_toggle.isChecked(),
            region=region,

            # For Wayland Region mode these are already selected.
            portal=wayland_portal,
            portal_stream=wayland_stream,
        )

        try:
            self.recorder.start(
                config,
                preview_cb=lambda frame: self.bridge.preview.emit(
                    frame.copy()
                ),
                state_cb=self.bridge.state.emit,
                error_cb=self.bridge.error.emit,
                finished_cb=self.bridge.finished.emit,
            )

        except Exception as exc:
            # Recorder did not take ownership, so clean up the portal.
            if wayland_stream is not None:
                try:
                    os.close(wayland_stream.fd)
                except OSError:
                    pass

            if wayland_portal is not None:
                wayland_portal.close()

            QMessageBox.critical(
                self,
                "Could not start recording",
                str(exc),
            )




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
