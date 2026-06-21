from __future__ import annotations

import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from pynput import keyboard as pynput_keyboard
from pynput.keyboard import Controller, Key


APP_TITLE = "AutoTyper"


class HotkeyBridge(QObject):
    start = pyqtSignal()
    toggle_pause = pyqtSignal()
    stop = pyqtSignal()


class DropEdit(QPlainTextEdit):
    fileDropped = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() or md.hasText():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            urls = md.urls()
            if urls:
                path = urls[0].toLocalFile()
                if path:
                    self.fileDropped.emit(path)
                    event.acceptProposedAction()
                    return
        if md.hasText():
            self.insertPlainText(md.text())
            event.acceptProposedAction()
            return
        super().dropEvent(event)


@dataclass
class TypingConfig:
    text: str
    wpm: int
    countdown: int


class TypingWorker(QObject):
    status = pyqtSignal(str)
    stats = pyqtSignal(int, int)
    finished = pyqtSignal(bool)

    def __init__(self, cfg: TypingConfig, stop_event, pause_event):
        super().__init__()
        self.cfg = cfg
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.controller = Controller()

    def _emit_stats(self, typed: int):
        self.stats.emit(typed, len(self.cfg.text))

    def _wait_while_paused(self) -> bool:
        while self.pause_event.is_set():
            if self.stop_event.is_set():
                return False
            time.sleep(0.05)
        return not self.stop_event.is_set()

    def _sleep_interruptible(self, seconds: float) -> bool:
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            if self.stop_event.is_set():
                return False
            if self.pause_event.is_set():
                if not self._wait_while_paused():
                    return False
                end = time.monotonic() + max(0.0, end - time.monotonic())
                continue
            remaining = end - time.monotonic()
            time.sleep(min(0.03, max(0.0, remaining)))
        return not self.stop_event.is_set()

    def _tap(self, key_or_char):
        if key_or_char == "\n":
            self.controller.press(Key.enter)
            self.controller.release(Key.enter)
        elif key_or_char == "\t":
            self.controller.press(Key.tab)
            self.controller.release(Key.tab)
        elif key_or_char == "\r":
            pass
        else:
            self.controller.press(key_or_char)
            self.controller.release(key_or_char)

    def run(self):
        try:
            if self.cfg.countdown > 0:
                for remaining in range(self.cfg.countdown, 0, -1):
                    if self.stop_event.is_set():
                        self.status.emit("Stopped")
                        self.finished.emit(False)
                        return
                    self.status.emit(f"Starting in {remaining}...")
                    for _ in range(20):
                        if not self._sleep_interruptible(0.05):
                            self.status.emit("Stopped")
                            self.finished.emit(False)
                            return

            if self.stop_event.is_set():
                self.status.emit("Stopped")
                self.finished.emit(False)
                return

            text = self.cfg.text
            wpm = max(1, int(self.cfg.wpm))
            cps = max(0.1, (wpm * 5.0) / 60.0)
            turbo = wpm >= 1000
            chunk_size = 1
            if turbo:
                chunk_size = max(8, min(32, wpm // 80))

            self.status.emit("Turbo burst" if turbo else "Typing...")
            typed = 0
            sleep_accum = 0
            chunk_delay = chunk_size / cps if turbo else 1.0 / cps

            for ch in text:
                if self.stop_event.is_set():
                    self.status.emit("Stopped")
                    self.finished.emit(False)
                    return

                if not self._wait_while_paused():
                    self.status.emit("Stopped")
                    self.finished.emit(False)
                    return

                self._tap(ch)
                typed += 1
                sleep_accum += 1

                if turbo:
                    if sleep_accum >= chunk_size:
                        delay = chunk_delay * random.uniform(0.82, 1.10)
                        if not self._sleep_interruptible(delay):
                            self.status.emit("Stopped")
                            self.finished.emit(False)
                            return
                        sleep_accum = 0
                else:
                    delay = chunk_delay * random.uniform(0.90, 1.12)
                    if not self._sleep_interruptible(delay):
                        self.status.emit("Stopped")
                        self.finished.emit(False)
                        return

                if typed % 50 == 0:
                    self._emit_stats(typed)

            self._emit_stats(typed)
            self.status.emit("Done")
            self.finished.emit(True)
        except Exception as exc:
            self.status.emit(f"Error: {exc}")
            self.finished.emit(False)


class RetroPanel(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("RetroPanel")
        self.vbox = QVBoxLayout(self)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(0)

        header = QLabel(title)
        header.setObjectName("PanelHeader")
        header.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.vbox.addWidget(header)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(10, 10, 10, 10)
        self.body_layout.setSpacing(8)
        self.vbox.addWidget(self.body)


class AutoTyperWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(560, 720)
        self.setMaximumWidth(620)

        self.stop_event = None
        self.pause_event = None
        self.thread: QThread | None = None
        self.worker: TypingWorker | None = None
        self.listener = None
        self.hotkey_bridge = HotkeyBridge()
        self.hotkey_bridge.start.connect(self.start_typing)
        self.hotkey_bridge.toggle_pause.connect(self.toggle_pause)
        self.hotkey_bridge.stop.connect(self.stop_typing)

        self._build_ui()
        self._connect_hotkeys()
        self._apply_style()

        self.refresh_status("Ready")
        self._update_counts()

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(12)

        title_bar = QFrame()
        title_bar.setObjectName("TitleBar")
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(10, 8, 10, 8)
        title_layout.setSpacing(8)

        title = QLabel("AutoTyper")
        title.setObjectName("AppTitle")
        subtitle = QLabel("Type anywhere with a classic desktop feel")
        subtitle.setObjectName("AppSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        title_layout.addWidget(title)
        title_layout.addStretch(1)
        title_layout.addWidget(subtitle)
        outer.addWidget(title_bar)

        self.input_panel = RetroPanel("Input")
        outer.addWidget(self.input_panel)

        self.editor = DropEdit()
        self.editor.setPlaceholderText("Drop a file here or paste code/text...")
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setTabStopDistance(32)
        self.editor.setMinimumHeight(250)
        self.editor.textChanged.connect(self._update_counts)
        self.editor.fileDropped.connect(self.load_file)
        self.input_panel.body_layout.addWidget(self.editor)

        file_row = QHBoxLayout()
        self.load_btn = QPushButton("Load File")
        self.clear_btn = QPushButton("Clear")
        file_row.addWidget(self.load_btn)
        file_row.addWidget(self.clear_btn)
        file_row.addStretch(1)
        self.input_panel.body_layout.addLayout(file_row)

        self.controls_panel = RetroPanel("Controls")
        outer.addWidget(self.controls_panel)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        self.wpm = QSpinBox()
        self.wpm.setRange(1, 2000)
        self.wpm.setValue(120)
        self.wpm.setSuffix(" WPM")

        self.countdown = QSpinBox()
        self.countdown.setRange(0, 30)
        self.countdown.setValue(3)
        self.countdown.setSuffix(" sec")

        self.turbo_label = QLabel("1000+ WPM = turbo burst")
        self.turbo_label.setObjectName("HintLabel")

        grid.addWidget(QLabel("Speed"), 0, 0)
        grid.addWidget(self.wpm, 0, 1)
        grid.addWidget(self.turbo_label, 0, 2)
        grid.addWidget(QLabel("Countdown"), 1, 0)
        grid.addWidget(self.countdown, 1, 1)

        self.controls_panel.body_layout.addLayout(grid)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.pause_btn = QPushButton("Pause")
        self.stop_btn = QPushButton("Stop")
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.pause_btn)
        btn_row.addWidget(self.stop_btn)
        self.controls_panel.body_layout.addLayout(btn_row)

        hotkeys = QLabel("Hotkeys: F8 Start   F9 Pause/Resume   F10 Stop")
        hotkeys.setObjectName("HotkeyLabel")
        self.controls_panel.body_layout.addWidget(hotkeys)

        self.stats_panel = RetroPanel("Session")
        outer.addWidget(self.stats_panel)

        stats_row = QHBoxLayout()
        self.char_count = QLabel("Chars: 0")
        self.line_count = QLabel("Lines: 0")
        self.mode_label = QLabel("Mode: Normal")
        stats_row.addWidget(self.char_count)
        stats_row.addWidget(self.line_count)
        stats_row.addWidget(self.mode_label)
        stats_row.addStretch(1)
        self.stats_panel.body_layout.addLayout(stats_row)

        self.status = QLabel("Ready")
        self.status.setObjectName("StatusLabel")
        self.stats_panel.body_layout.addWidget(self.status)

        footer = QLabel("Tip: keep the cursor focused in the target app before typing starts.")
        footer.setObjectName("FooterLabel")
        outer.addWidget(footer)

        self.load_btn.clicked.connect(self.open_file_dialog)
        self.clear_btn.clicked.connect(self.clear_text)
        self.start_btn.clicked.connect(self.start_typing)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.stop_btn.clicked.connect(self.stop_typing)

        self.shortcut_start = QShortcut(QKeySequence("F8"), self)
        self.shortcut_pause = QShortcut(QKeySequence("F9"), self)
        self.shortcut_stop = QShortcut(QKeySequence("F10"), self)
        self.shortcut_start.activated.connect(self.start_typing)
        self.shortcut_pause.activated.connect(self.toggle_pause)
        self.shortcut_stop.activated.connect(self.stop_typing)

    def _apply_style(self):
        QApplication.setStyle("Fusion")
        self.setStyleSheet(
            """
            QWidget#Root {
                background: #ead7c5;
                color: #27334a;
                font-family: "Courier New";
                font-size: 10pt;
            }

            QFrame#TitleBar {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #ff9cb8,
                    stop:1 #f4c0d0
                );
                border: 2px solid #7c5a63;
                border-radius: 0px;
                min-height: 24px;
                max-height: 24px;
            }

            QLabel#AppTitle {
                color: #31415a;
                font-family: "Courier New";
                font-size: 11pt;
                font-weight: 700;
                padding: 0px;
                margin: 0px;
            }

            QLabel#AppSubtitle {
                color: #4d5d72;
                font-family: "Courier New";
                font-size: 8pt;
                padding: 0px;
                margin: 0px;
            }

            QFrame#RetroPanel {
                background: #f7efe7;
                border: 2px solid #7b8aa4;
                border-radius: 0px;
                margin: 0px;
                padding: 0px;
            }

            QLabel#PanelHeader {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #ff8f6a,
                    stop:1 #ffb67a
                );
                color: #29354a;
                border-bottom: 2px solid #7b8aa4;
                border-radius: 0px;
                padding: 2px 6px;
                font-family: "Courier New";
                font-size: 9pt;
                font-weight: 700;
            }

            QLabel, QCheckBox, QRadioButton, QGroupBox {
                color: #27334a;
                font-family: "Courier New";
            }

            QLabel#HintLabel,
            QLabel#HotkeyLabel,
            QLabel#FooterLabel {
                color: #5c667a;
                font-family: "Courier New";
                font-size: 8pt;
            }

            QLabel#StatusLabel {
                background: #edf2fb;
                border: 2px solid #7b8aa4;
                border-radius: 0px;
                padding: 5px 7px;
                color: #203049;
                font-family: "Courier New";
                font-size: 9pt;
                font-weight: 700;
            }

            QPlainTextEdit, QTextEdit {
                background: #fffdf8;
                color: #27334a;
                border: 2px solid #6f809b;
                border-radius: 0px;
                padding: 6px;
                selection-background-color: #95b5d6;
                selection-color: #27334a;
                font-family: "Consolas";
                font-size: 10pt;
            }

            QPlainTextEdit:focus, QTextEdit:focus {
                border: 2px solid #31415a;
            }

            QPlainTextEdit QScrollBar:vertical,
            QTextEdit QScrollBar:vertical,
            QAbstractScrollArea QScrollBar:vertical {
                width: 14px;
                background: #f7efe7;
                margin: 0px;
            }

            QPlainTextEdit QScrollBar::handle:vertical,
            QTextEdit QScrollBar::handle:vertical,
            QAbstractScrollArea QScrollBar::handle:vertical {
                background: #7b8aa4;
                min-height: 18px;
                border: 2px solid #6f809b;
                border-radius: 0px;
            }

            QLineEdit,
            QSpinBox,
            QDoubleSpinBox,
            QComboBox {
                background: #fffdf8;
                color: #27334a;
                border: 2px solid #6f809b;
                border-radius: 0px;
                padding: 3px 6px;
                min-height: 22px;
                font-family: "Courier New";
            }

            QComboBox::drop-down {
                border-left: 2px solid #6f809b;
                width: 18px;
                border-radius: 0px;
            }

            QPushButton {
                background: #e7edf6;
                color: #1f2e44;
                border: 2px solid #6f809b;
                border-radius: 0px;
                border-bottom: 3px solid #55657d;
                border-right: 3px solid #55657d;
                padding: 4px 10px;
                min-height: 22px;
                min-width: 78px;
                font-family: "Courier New";
                font-size: 9pt;
                font-weight: 700;
            }

            QPushButton:hover {
                background: #f4f7fb;
            }

            QPushButton:pressed {
                border-top: 3px solid #55657d;
                border-left: 3px solid #55657d;
                border-bottom: 2px solid #6f809b;
                border-right: 2px solid #6f809b;
                padding-top: 6px;
                padding-left: 11px;
            }

            QPushButton:disabled {
                color: #7f8693;
                background: #d6dce5;
            }

            QCheckBox {
                spacing: 8px;
                font-family: "Courier New";
                color: #27334a;
            }

            QCheckBox::indicator {
                width: 14px;
                height: 14px;
                border: 2px solid #6f809b;
                background: #fffdf8;
                border-radius: 0px;
            }

            QCheckBox::indicator:checked {
                background: #95b5d6;
            }

            QSlider::groove:horizontal {
                height: 8px;
                background: #d6dce5;
                border: 2px solid #6f809b;
                border-radius: 0px;
            }

            QSlider::handle:horizontal {
                background: #ff9cb8;
                border: 2px solid #7c5a63;
                width: 16px;
                margin: -6px 0;
                border-radius: 0px;
            }

            QTabBar::tab {
                background: #e7edf6;
                color: #27334a;
                border: 2px solid #6f809b;
                border-bottom: 0px;
                padding: 4px 8px;
                margin-right: 2px;
                border-radius: 0px;
                font-family: "Courier New";
                font-size: 9pt;
            }

            QTabBar::tab:selected {
                background: #f7efe7;
                border-color: #31415a;
            }

            QTabWidget::pane {
                border: 2px solid #6f809b;
                top: -2px;
                border-radius: 0px;
                background: #f7efe7;
            }

            QToolTip {
                background: #f7efe7;
                color: #27334a;
                border: 2px solid #7b8aa4;
                padding: 4px 6px;
                border-radius: 0px;
                font-family: "Courier New";
            }

            QMenuBar, QMenu, QStatusBar {
                background: #f7efe7;
                color: #27334a;
                font-family: "Courier New";
            }

            QMenu {
                border: 2px solid #7b8aa4;
                border-radius: 0px;
            }

            QMenu::item:selected {
                background: #95b5d6;
            }
            """
        )

    def _set_mode_label(self):
        if self.wpm.value() >= 1000:
            self.mode_label.setText("Mode: Turbo")
        else:
            self.mode_label.setText("Mode: Normal")

    def _update_counts(self):
        text = self.editor.toPlainText()
        chars = len(text)
        lines = text.count("\n") + (1 if text else 0)
        self.char_count.setText(f"Chars: {chars}")
        self.line_count.setText(f"Lines: {lines}")
        self._set_mode_label()

    def refresh_status(self, text: str):
        self.status.setText(text)

    def clear_text(self):
        self.editor.clear()
        self._update_counts()
        self.refresh_status("Cleared")

    def open_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open file",
            "",
            "Text / Code Files (*.txt *.py *.js *.ts *.java *.cpp *.c *.cs *.html *.css *.json *.md);;All Files (*.*)",
        )
        if path:
            self.load_file(path)

    def load_file(self, path: str):
        p = Path(path)
        try:
            data = p.read_text(encoding="utf-8")
        except Exception:
            try:
                data = p.read_text(encoding="latin-1")
            except Exception as exc:
                QMessageBox.critical(self, "Load failed", f"Could not open file:\n{exc}")
                return
        self.editor.setPlainText(data)
        self._update_counts()
        self.refresh_status(f"Loaded {p.name}")

    def _ensure_idle(self) -> bool:
        if self.thread and self.thread.isRunning():
            return False
        return True

    def _make_events(self):
        import threading

        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.clear()

    def start_typing(self):
        if not self._ensure_idle():
            self.refresh_status("Already running")
            return

        text = self.editor.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "Nothing to type", "Enter or load some text first.")
            return

        self._make_events()
        cfg = TypingConfig(
            text=text,
            wpm=int(self.wpm.value()),
            countdown=int(self.countdown.value()),
        )

        self.thread = QThread(self)
        self.worker = TypingWorker(cfg, self.stop_event, self.pause_event)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.status.connect(self.refresh_status)
        self.worker.stats.connect(self._on_stats)
        self.worker.finished.connect(self._worker_finished)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def _on_stats(self, typed: int, total: int):
        self.char_count.setText(f"Chars: {total}")
        self.refresh_status(f"Typing... {typed}/{total}")

    def toggle_pause(self):
        if not self.pause_event or not self.thread or not self.thread.isRunning():
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.setText("Pause")
            self.refresh_status("Resumed")
        else:
            self.pause_event.set()
            self.pause_btn.setText("Resume")
            self.refresh_status("Paused")

    def stop_typing(self):
        if self.stop_event is not None:
            self.stop_event.set()
        if self.pause_event is not None:
            self.pause_event.clear()
        self.pause_btn.setText("Pause")
        self.refresh_status("Stopping...")

    def _worker_finished(self, ok: bool):
        if ok:
            self.refresh_status("Done")
        elif self.stop_event is not None and self.stop_event.is_set():
            self.refresh_status("Stopped")
        self.pause_btn.setText("Pause")

    def _connect_hotkeys(self):
        try:
            self.listener = pynput_keyboard.Listener(on_release=self._on_hotkey_release)
            self.listener.daemon = True
            self.listener.start()
        except Exception:
            self.listener = None

    def _on_hotkey_release(self, key):
        try:
            if key == pynput_keyboard.Key.f8:
                self.hotkey_bridge.start.emit()
            elif key == pynput_keyboard.Key.f9:
                self.hotkey_bridge.toggle_pause.emit()
            elif key == pynput_keyboard.Key.f10:
                self.hotkey_bridge.stop.emit()
        except Exception:
            pass

    def closeEvent(self, event):
        try:
            self.stop_typing()
            if self.listener is not None:
                self.listener.stop()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    win = AutoTyperWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
