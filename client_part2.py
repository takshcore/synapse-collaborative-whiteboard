"""
client_part2.py — GUI Layer
=====================================
Contains: LoginDialog, CanvasWidget, ParticipantPanel, MainWindow, main()

ARCHITECTURE — Separation of Concerns (Viva Talking Point):
------------------------------------------------------------
This file is the PRESENTATION LAYER only. It:
  - Renders the UI using Qt widgets
  - Translates user gestures into network events
  - Reacts to network events by updating the UI

It does NOT contain any networking or async code.
All network I/O lives in client_part1.py (the LOGIC LAYER).
The two layers communicate exclusively through SignalBridge — a
one-directional signal bus that enforces thread safety automatically.

WHY PyQt6?
  PyQt6 is a Python binding for the Qt6 C++ framework. Qt provides:
  - A mature cross-platform widget toolkit
  - A built-in event loop (QApplication.exec())
  - Thread-safe signal-slot communication (QThread + pyqtSignal)
  - QPainter for hardware-accelerated 2D rendering
"""
import sys
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QColorDialog, QSlider, QSplitter,
    QDialogButtonBox, QFrame, QSizePolicy)
from PyQt6.QtCore import Qt, QPoint, QTimer, QSize
from PyQt6.QtGui import QPainter, QPen, QPixmap, QColor, QFont, QCursor

from client_part1 import (
    SignalBridge, SocketIOWorker, WebRTCWorker)


# ── LoginDialog ───────────────────────────────────────────────────
# The login dialog is shown BEFORE the main window is created.
# It collects the three parameters needed to bootstrap both workers:
#   1. Name     → displayed in the participant panel
#   2. Room Code → determines which SocketIO room to join
#   3. Server IP → allows any host to run the server (no hardcoded URL)
# This "configurable server" design means the same .exe works for
# any room on any server — just type a different IP.
class LoginDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Join Whiteboard")
        self.setFixedSize(380, 290)
        self.setStyleSheet("""
            QDialog { background:#1a1a2e; color:#e0e0e0; }
            QLabel  { color:#a0a0c0; font-size:12px; }
            QLineEdit {
                background:#16213e; border:1px solid #0f3460;
                border-radius:6px; padding:8px; color:#fff; font-size:13px; }
            QLineEdit:focus { border-color:#e94560; }
            QPushButton {
                background:#e94560; color:#fff; border-radius:6px;
                padding:10px; font-size:13px; font-weight:bold; }
            QPushButton:hover { background:#c73652; }
        """)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(30, 25, 30, 25)

        title = QLabel("🎨  Collaborative Whiteboard")
        title.setStyleSheet("font-size:16px;font-weight:bold;color:#e94560;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        layout.addWidget(QLabel("Display Name"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Your name…")
        layout.addWidget(self.name_edit)

        layout.addWidget(QLabel("Room Code"))
        self.room_edit = QLineEdit()
        self.room_edit.setPlaceholderText("e.g. ROOM42")
        layout.addWidget(self.room_edit)

        layout.addWidget(QLabel("Server IP : Port"))
        self.server_edit = QLineEdit()
        self.server_edit.setText("192.168.137.208:5000")
        self.server_edit.setPlaceholderText("e.g. 192.168.1.5:5000")
        layout.addWidget(self.server_edit)

        btn = QPushButton("Join / Create Room")
        btn.clicked.connect(self._validate)
        layout.addWidget(btn)

    def _validate(self):
        if self.name_edit.text().strip() and self.room_edit.text().strip() \
                and self.server_edit.text().strip():
            self.accept()
        else:
            QMessageBox.warning(self, "Missing Info", "Please fill in all fields.")

    def get_values(self):
        ip_port = self.server_edit.text().strip()
        if not ip_port.startswith('http'):
            ip_port = f"http://{ip_port}"
        return (
            self.name_edit.text().strip(),
            self.room_edit.text().strip().upper(),
            ip_port,
        )


# ── CanvasWidget ──────────────────────────────────────────────────
class CanvasWidget(QWidget):
    """
    Custom drawing canvas using QPainter and QPixmap as a backing store.

    WHY QPixmap as a backing store? (Viva Talking Point)
    Qt repaints widgets frequently (on resize, expose, minimize/restore).
    If we drew directly to the screen without a backing store, ALL previous
    strokes would be lost on every repaint. By maintaining a QPixmap:
      - paintEvent simply copies the pixmap to screen (O(1) blit)
      - Strokes are permanently committed to the pixmap immediately
      - Remote strokes (draw_remote_stroke) paint into the same pixmap
    This is the classic "off-screen buffer" pattern for persistent drawing.

    WHY not a QGraphicsScene?
    QGraphicsScene stores individual items that can be selected/moved.
    Our canvas treats strokes as permanent marks (like a real whiteboard)
    — no need for item management overhead.

    can_draw: controlled by MainWindow based on token ownership.
    When False, all mouse events are silently ignored — only the
    token holder can draw.
    """
    # Eraser hack: background color that matches the canvas fill
    ERASER_COLOR = "#ffffff"
    ERASER_WIDTH = 25

    def __init__(self, parent=None):
        super().__init__(parent)
        self.can_draw = False
        self.color = QColor("#e94560")
        self.stroke_width = 4
        # When True, drawing uses ERASER_COLOR + ERASER_WIDTH instead of
        # the user's chosen color/width.  Toggled by the eraser button.
        self.is_eraser_mode = False
        self._last_pt: QPoint | None = None
        self._current_stroke: list[dict] = []
        self._pixmap = QPixmap(3000, 3000)
        self._pixmap.fill(QColor(self.ERASER_COLOR))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self._send_stroke_cb = None   # set by MainWindow
        # ── Image placement state ────────────────────────────────
        self._image_placement_mode = False   # True while waiting for click
        self._pending_image_b64: str | None = None  # compressed b64 string
        self._image_place_cb = None          # called with (b64, x, y) on place

    def set_stroke_callback(self, cb):
        """cb(stroke_dict) called when a local stroke finishes."""
        self._send_stroke_cb = cb

    def paintEvent(self, event):
        """
        Qt calls this whenever the widget needs repainting.
        We simply blit the QPixmap backing store onto the widget surface.
        This is O(1) regardless of how many strokes have been drawn.
        The semi-transparent overlay when can_draw=False gives a visual
        cue that this client doesn't have the drawing token.
        """
        p = QPainter(self)
        p.drawPixmap(0, 0, self._pixmap)
        if not self.can_draw:
            p.fillRect(self.rect(), QColor(0, 0, 0, 40))

    def mousePressEvent(self, e):
        if not self.can_draw: return
        if e.button() == Qt.MouseButton.LeftButton:
            # ── Image placement mode: stamp on click, then exit mode ────
            if self._image_placement_mode and self._pending_image_b64:
                pt = e.position().toPoint()
                self._draw_image_at(self._pending_image_b64, pt.x(), pt.y())
                if self._image_place_cb:
                    self._image_place_cb(self._pending_image_b64, pt.x(), pt.y())
                # Exit placement mode
                self._image_placement_mode = False
                self._pending_image_b64 = None
                self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
                return
            # ── Normal drawing ────────────────────────────────────
            self._last_pt = e.position().toPoint()
            self._current_stroke = [{
                'x': self._last_pt.x(), 'y': self._last_pt.y()}]

    def _effective_color(self) -> QColor:
        """Return the color to use for the current stroke.
        In eraser mode this is the canvas background, overriding whatever
        color the user has picked — the server sees it as a normal stroke."""
        return QColor(self.ERASER_COLOR) if self.is_eraser_mode else self.color

    def _effective_width(self) -> int:
        """Return the stroke width for the current stroke.
        In eraser mode a thick fixed width is used so the 'eraser' covers
        a visible area regardless of the slider setting."""
        return self.ERASER_WIDTH if self.is_eraser_mode else self.stroke_width

    def mouseMoveEvent(self, e):
        if not self.can_draw or self._last_pt is None: return
        new_pt = e.position().toPoint()
        # Resolve effective drawing parameters once per segment so both
        # the local render and the network payload stay in sync.
        eff_color = self._effective_color()
        eff_width = self._effective_width()
        self._draw_line(self._last_pt, new_pt, eff_color, eff_width)
        self._current_stroke.append({'x': new_pt.x(), 'y': new_pt.y()})
        self._last_pt = new_pt

    def mouseReleaseEvent(self, e):
        """
        On mouse-up, the completed stroke is sent to the server.
        WHY send only on release (not on every mouseMoveEvent)?
        Sending individual segments every few pixels would flood the
        server. Instead, we accumulate all points locally during the drag
        and send the complete stroke as a single event. This batching
        reduces network messages by ~100x for a typical stroke.
        """
        if not self.can_draw: return
        if e.button() == Qt.MouseButton.LeftButton and self._current_stroke:
            if self._send_stroke_cb:
                # Send the hijacked color/width so the server broadcasts
                # the eraser stroke as a thick background-colored pen stroke.
                self._send_stroke_cb({
                    'points': self._current_stroke,
                    'color': self._effective_color().name(),
                    'width': self._effective_width(),
                })
            self._last_pt = None
            self._current_stroke = []

    def _draw_line(self, p1: QPoint, p2: QPoint, color: QColor, width: int):
        painter = QPainter(self._pixmap)
        pen = QPen(color, width, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(p1, p2)
        painter.end()
        self.update()

    def draw_remote_stroke(self, stroke: dict):
        """
        Render a stroke received from a remote peer onto the backing pixmap.

        WHY identical rendering code for local and remote strokes?
        Consistency. Both paths use the same QPainter + QPen logic, so
        the canvas looks identical on every client regardless of who drew.
        Color and width are embedded in the stroke dict by the sender,
        so the eraser (which sends ERASER_COLOR strokes) also replicates
        correctly on remote canvases without any special-case logic.
        """
        pts = stroke.get('points', [])
        color = QColor(stroke.get('color', '#ffffff'))
        width = stroke.get('width', 4)
        if len(pts) < 2:
            return
        painter = QPainter(self._pixmap)
        pen = QPen(color, width, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        for i in range(1, len(pts)):
            p1 = QPoint(pts[i-1]['x'], pts[i-1]['y'])
            p2 = QPoint(pts[i]['x'], pts[i]['y'])
            painter.drawLine(p1, p2)
        painter.end()
        self.update()

    def clear_canvas(self):
        # Use the class constant so the fill always matches ERASER_COLOR.
        self._pixmap.fill(QColor(self.ERASER_COLOR))
        self.update()

    # ── Image stamping ───────────────────────────────────────
    def _draw_image_at(self, b64_data: str, x: int, y: int):
        """Decode a Base64 PNG/JPEG string and stamp it onto the backing pixmap."""
        import base64
        from PyQt6.QtGui import QImage
        raw = base64.b64decode(b64_data)
        qimg = QImage()
        qimg.loadFromData(raw)
        if qimg.isNull():
            return
        painter = QPainter(self._pixmap)
        painter.drawImage(QPoint(x, y), qimg)
        painter.end()
        self.update()

    def draw_remote_image(self, data: dict):
        """Public slot — called when a remote 'image' event arrives via bridge."""
        self._draw_image_at(
            data.get('base64_data', ''),
            data.get('x', 0),
            data.get('y', 0),
        )


# ── ParticipantPanel ──────────────────────────────────────────────
class ParticipantPanel(QWidget):
    """Left sidebar: participant list + host action buttons."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(200)
        self.setStyleSheet("background:#12122a; border-right:1px solid #1e1e40;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(6)

        lbl = QLabel("👥  Participants")
        lbl.setStyleSheet("color:#a0a0c0; font-size:11px; font-weight:bold;")
        layout.addWidget(lbl)

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("""
            QListWidget { background:#0d0d1a; border:none; color:#e0e0e0; }
            QListWidget::item { padding:6px 4px; border-radius:4px; }
            QListWidget::item:selected { background:#1e1e50; }
        """)
        layout.addWidget(self.list_widget)

        self.force_mute_btn = QPushButton("🔇 Force Mute")
        self.ask_unmute_btn = QPushButton("🎙 Ask Unmute")
        for btn in (self.force_mute_btn, self.ask_unmute_btn):
            btn.setStyleSheet("""
                QPushButton { background:#1e1e40; color:#a0a0c0; border-radius:5px;
                              padding:6px; font-size:11px; }
                QPushButton:hover { background:#e94560; color:#fff; }
            """)
        layout.addWidget(self.force_mute_btn)
        layout.addWidget(self.ask_unmute_btn)

        self._host_buttons_visible = False
        self.force_mute_btn.hide()
        self.ask_unmute_btn.hide()

        self._participants: dict = {}
        self._my_sid: str = ""
        self._host_sid: str = ""
        self._token_sid: str | None = None

    def show_host_controls(self, visible: bool):
        self._host_buttons_visible = visible
        if visible:
            self.force_mute_btn.show()
            self.ask_unmute_btn.show()

    def update_participants(self, data: dict):
        self._participants = data.get('participants', {})
        self._host_sid = data.get('host_sid', '')
        self._token_sid = data.get('token_holder_sid')
        self._refresh_list()

    def _refresh_list(self):
        self.list_widget.clear()
        for sid, info in self._participants.items():
            icons = ""
            if sid == self._host_sid:
                icons += "👑"
            if info.get('is_muted'):
                icons += "🔇"
            if info.get('hand_raised'):
                icons += "✋"
            if sid == self._token_sid:
                icons += "✏️"
            label = f"{icons} {info['name']}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, sid)
            self.list_widget.addItem(item)

    def selected_sid(self) -> str | None:
        item = self.list_widget.currentItem()
        if item:
            return item.data(Qt.ItemDataRole.UserRole)
        return None


# ── MainWindow ────────────────────────────────────────────────────
DARK = "#0d0d1a"
MID  = "#12122a"
ACC  = "#e94560"

TOOLBAR_STYLE = """
QPushButton {
    background:#1a1a3a; color:#c0c0e0; border-radius:6px;
    padding:8px 14px; font-size:12px; margin:2px;
}
QPushButton:hover  { background:#e94560; color:#fff; }
QPushButton:checked { background:#e94560; color:#fff; }
QPushButton:disabled { background:#111128; color:#444; }
"""


class MainWindow(QMainWindow):
    """
    The main application window. Acts as the CONTROLLER in an MVC pattern.

    Responsibilities:
      1. Assembles all widgets (ParticipantPanel, CanvasWidget, toolbar).
      2. Creates SignalBridge, SocketIOWorker, and WebRTCWorker.
      3. Connects SignalBridge signals to UI update slots (main thread).
      4. Routes user actions to SocketIOWorker.emit_event() calls.

    WHY start WebRTCWorker BEFORE SocketIOWorker? (Viva Talking Point)
    The WebRTC worker must have its asyncio loop running BEFORE SocketIO
    connects. If SocketIO connects first, 'webrtc_initiate' signals can
    arrive before the WebRTC loop is ready, causing the race condition
    that was fixed by the _loop_ready threading.Event in WebRTCWorker.
    """
    def __init__(self, name: str, room_code: str, server_url: str):
        super().__init__()
        self.my_name = name
        self.room_code = room_code
        self.server_url = server_url
        self.my_sid = ""
        self.host_sid = ""
        self.token_holder_sid = None
        self.is_host = False
        self.is_muted = False
        self.hand_raised = False
        self._participants: dict = {}

        self.setWindowTitle(f"Whiteboard — Room {room_code}")
        self.resize(1100, 720)
        self.setStyleSheet(f"QMainWindow {{ background:{DARK}; }}")

        # ── Bridge + Workers ──────────────────────────────────────
        self.bridge = SignalBridge()
        self.sio_worker = SocketIOWorker(self.bridge, room_code, name, server_url)

        # WebRTC audio worker — wrapped in try/except because aiortc and
        # sounddevice can fail on newer Python versions (e.g. 3.14).
        # If it fails, the app still works fully for drawing; only voice
        # chat is unavailable.
        try:
            self.rtc_worker = WebRTCWorker(self.bridge, self.sio_worker, room_code)
            self._audio_available = True
        except Exception as e:
            self.rtc_worker = None
            self._audio_available = False
            print(f"[Audio] WebRTC unavailable: {e}")

        # ── Build UI ──────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.panel = ParticipantPanel()
        root.addWidget(self.panel)

        right = QWidget()
        right.setStyleSheet(f"background:{DARK};")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.status_bar_lbl = QLabel(f"  Connecting…  |  Room: {room_code}")
        self.status_bar_lbl.setStyleSheet(
            f"background:{MID}; color:#a0a0c0; padding:6px; font-size:11px;")
        right_layout.addWidget(self.status_bar_lbl)

        self.canvas = CanvasWidget()
        self.canvas.set_stroke_callback(self._on_local_stroke)
        right_layout.addWidget(self.canvas, 1)

        toolbar = self._build_toolbar()
        right_layout.addWidget(toolbar)
        root.addWidget(right, 1)

        # ── Connect bridge signals → slots ────────────────────────
        self.bridge.connected.connect(self._on_connected)
        self.bridge.disconnected.connect(self._on_disconnected)
        self.bridge.error_occurred.connect(self._on_error)
        self.bridge.room_joined.connect(self._on_room_joined)
        self.bridge.draw_received.connect(self.canvas.draw_remote_stroke)
        self.bridge.canvas_cleared.connect(self.canvas.clear_canvas)
        self.bridge.token_request_incoming.connect(self._on_token_request)
        self.bridge.token_update.connect(self._on_token_update)
        self.bridge.token_denied.connect(self._on_token_denied)
        self.bridge.participant_update.connect(self._on_participant_update)
        self.bridge.peer_left.connect(self._on_peer_left)
        self.bridge.you_are_muted.connect(self._on_force_muted)
        self.bridge.unmute_request.connect(self._on_unmute_request)
        # Image: route incoming images directly to canvas draw method
        self.bridge.image_received.connect(self.canvas.draw_remote_image)
        # Wire up the placement callback so placed images are sent to server
        self.canvas._image_place_cb = self._on_image_placed

        # Host panel buttons
        self.panel.force_mute_btn.clicked.connect(self._host_force_mute)
        self.panel.ask_unmute_btn.clicked.connect(self._host_ask_unmute)

        # ── Start workers ─────────────────────────────────────────
        if self.rtc_worker:
            try:
                self.rtc_worker.start()
            except Exception as e:
                self.rtc_worker = None
                self._audio_available = False
                print(f"[Audio] Failed to start WebRTC worker: {e}")
        self.sio_worker.start()

    # ── Toolbar ───────────────────────────────────────────────────
    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(52)
        bar.setStyleSheet(f"background:{MID}; border-top:1px solid #1e1e40;")
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 4, 10, 4)
        h.setSpacing(4)

        self.mute_btn = QPushButton("🎙 Mute")
        self.mute_btn.setCheckable(True)
        self.mute_btn.clicked.connect(self._toggle_mute)

        self.hand_btn = QPushButton("✋ Raise Hand")
        self.hand_btn.setCheckable(True)
        self.hand_btn.clicked.connect(self._toggle_hand)

        self.request_btn = QPushButton("✏️ Request Token")
        self.request_btn.clicked.connect(self._request_token)

        self.drop_btn = QPushButton("⬇ Drop Token")
        self.drop_btn.clicked.connect(self._drop_token)
        self.drop_btn.setEnabled(False)

        color_btn = QPushButton("🎨 Color")
        color_btn.clicked.connect(self._pick_color)

        self.width_slider = QSlider(Qt.Orientation.Horizontal)
        self.width_slider.setRange(1, 20)
        self.width_slider.setValue(4)
        self.width_slider.setFixedWidth(90)
        self.width_slider.setStyleSheet(
            "QSlider::groove:horizontal { height:4px; background:#1e1e40; }"
            "QSlider::handle:horizontal { background:#e94560; width:12px;"
            "  height:12px; margin:-4px 0; border-radius:6px; }")
        self.width_slider.valueChanged.connect(
            lambda v: setattr(self.canvas, 'stroke_width', v))

        # ── Eraser button ─────────────────────────────────────────
        # Checkable so QPushButton:checked CSS highlights it automatically.
        self.eraser_btn = QPushButton("🧽 Eraser")
        self.eraser_btn.setCheckable(True)
        self.eraser_btn.clicked.connect(self._toggle_eraser)

        # ── Insert Image button ──────────────────────────────────
        self.img_btn = QPushButton("🖼️ Image")
        self.img_btn.clicked.connect(self._on_insert_image)

        self.clear_btn = QPushButton("🗑 Clear")
        self.clear_btn.clicked.connect(self._clear_canvas)
        self.clear_btn.setEnabled(False)

        for w in (self.mute_btn, self.hand_btn, self.request_btn,
                  self.drop_btn, color_btn, self.width_slider,
                  self.eraser_btn, self.img_btn, self.clear_btn):
            if isinstance(w, QPushButton):
                w.setStyleSheet(TOOLBAR_STYLE)
            h.addWidget(w)

        h.addStretch()
        self.token_lbl = QLabel("Token: —")
        self.token_lbl.setStyleSheet("color:#a0a0c0; font-size:11px;")
        h.addWidget(self.token_lbl)
        return bar

    # ── Bridge signal slots (all run on main thread) ──────────────

    def _on_connected(self):
        self.status_bar_lbl.setText(
            f"  🟢 Connected  |  Room: {self.room_code}  |  You: {self.my_name}")

    def _on_disconnected(self):
        self.status_bar_lbl.setText("  🔴 Disconnected")

    def _on_error(self, msg: str):
        QMessageBox.critical(self, "Connection Error", msg)

    def _on_room_joined(self, data: dict):
        """
        Slot fired when the server confirms room entry.

        This is the most important UI event — it transitions the app from
        'Connecting...' to fully operational state:
          1. Stores our identity (SID, role, host status).
          2. Populates the participants panel.
          3. Replays the canvas history (late-joiner support).
          4. Updates token UI to enable/disable drawing controls.

        WHY replay canvas here and not on 'draw_stroke' events?
        When a late joiner connects, the server sends all historical
        strokes in the 'room_joined' payload. We replay them once here
        rather than relying on the live event stream, which only delivers
        new strokes going forward.
        """
        self.my_sid = data['your_sid']
        self.host_sid = data['host_sid']
        self.token_holder_sid = data.get('token_holder_sid')
        self.is_host = (data['role'] == 'host')

        self._participants = data.get('participants', {})
        self.panel.update_participants({
            'participants': self._participants,
            'host_sid': self.host_sid,
            'token_holder_sid': self.token_holder_sid,
        })
        self.panel.show_host_controls(self.is_host)
        self.clear_btn.setEnabled(self.is_host)

        # Replay canvas strokes/images for late joiners
        for item in data.get('canvas_strokes', []):
            if item.get('type') == 'image':
                self.canvas.draw_remote_image(item)
            else:
                self.canvas.draw_remote_stroke(item)

        self._update_token_ui()
        role_str = "Host 👑" if self.is_host else "Participant"
        self.status_bar_lbl.setText(
            f"  🟢 {self.my_name} ({role_str})  |  Room: {self.room_code}")

    def _on_participant_update(self, data: dict):
        self._participants = data.get('participants', {})
        self.host_sid = data.get('host_sid', self.host_sid)
        self.token_holder_sid = data.get('token_holder_sid')
        self.panel.update_participants(data)
        self._update_token_ui()
        # Sync own mute state in case server updated it
        me = self._participants.get(self.my_sid, {})
        if me.get('is_muted') != self.is_muted:
            self.is_muted = me.get('is_muted', self.is_muted)
            self.mute_btn.setChecked(self.is_muted)
            if self.rtc_worker.mic_track:
                self.rtc_worker.mic_track.is_muted = self.is_muted

    def _on_peer_left(self, sid: str):
        pass  # participant_update handles UI; WebRTCWorker cleans up PC

    def _on_token_request(self, requester_sid: str, requester_name: str):
        """
        We are the current token holder. Show Yes/No dialog.
        This slot runs on the main thread — safe to show QMessageBox.
        """
        reply = QMessageBox.question(
            self, "Token Request",
            f"✋  {requester_name} wants to draw.\n\nApprove?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        approved = reply == QMessageBox.StandardButton.Yes
        self.sio_worker.emit_event('approve_token', {
            'room_code': self.room_code,
            'requester_sid': requester_sid,
            'approved': approved,
        })

    def _on_token_update(self, data: dict):
        self.token_holder_sid = data.get('token_holder_sid')
        new_parts = data.get('participants')
        if new_parts:
            self._participants = new_parts
            self.panel.update_participants({
                'participants': new_parts,
                'host_sid': self.host_sid,
                'token_holder_sid': self.token_holder_sid,
            })
        self._update_token_ui()

    def _on_token_denied(self, reason: str):
        QMessageBox.information(self, "Token Denied", reason)

    def _on_force_muted(self):
        """Server force-muted us."""
        self.is_muted = True
        self.mute_btn.setChecked(True)
        self.mute_btn.setText("🔇 Muted")
        if self.rtc_worker.mic_track:
            self.rtc_worker.mic_track.is_muted = True
        QMessageBox.information(self, "Muted", "The host has muted your microphone.")

    def _on_unmute_request(self):
        """Host asked us to unmute."""
        reply = QMessageBox.question(
            self, "Unmute Request",
            "The host has requested you to unmute.\n\nAllow?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        accepted = reply == QMessageBox.StandardButton.Yes
        if accepted:
            self.is_muted = False
            self.mute_btn.setChecked(False)
            self.mute_btn.setText("🎙 Mute")
            if self.rtc_worker.mic_track:
                self.rtc_worker.mic_track.is_muted = False
        self.sio_worker.emit_event('unmute_response', {
            'room_code': self.room_code,
            'accepted': accepted,
        })

    # ── User action handlers ──────────────────────────────────────

    def _on_local_stroke(self, stroke: dict):
        self.sio_worker.emit_event('draw_stroke', {
            'room_code': self.room_code,
            'stroke': stroke,
        })

    def _on_insert_image(self):
        """Open a file dialog, compress the image, and enter placement mode.
        The user then clicks on the canvas to stamp the image at that position."""
        if not self.canvas.can_draw:
            QMessageBox.warning(self, "No Token",
                                "You need the drawing token to insert images.")
            return

        from PyQt6.QtWidgets import QFileDialog
        from PyQt6.QtGui import QImage
        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
        import base64

        path, _ = QFileDialog.getOpenFileName(
            self, "Select Image", "", "Images (*.png *.jpg *.jpeg)")
        if not path:
            return

        img = QImage(path)
        if img.isNull():
            QMessageBox.warning(self, "Error", "Could not load the selected image.")
            return

        # Scale down to max 800px wide to keep payload small over the network
        if img.width() > 800:
            img = img.scaledToWidth(800, Qt.TransformationMode.SmoothTransformation)

        # Encode to PNG bytes → Base64 string
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        b64 = base64.b64encode(buf.data().data()).decode('utf-8')
        buf.close()

        # Enter placement mode — next canvas click stamps the image
        self.canvas._pending_image_b64 = b64
        self.canvas._image_placement_mode = True
        self.canvas.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.status_bar_lbl.setText(
            "  🖼️ Click on the canvas to place the image…")

    def _on_image_placed(self, b64: str, x: int, y: int):
        """Called by the canvas after the user clicks to place an image.
        Sends the payload to the server which broadcasts it to all peers."""
        self.sio_worker.emit_event('insert_image', {
            'room_code': self.room_code,
            'base64_data': b64,
            'x': x,
            'y': y,
        })
        # Restore the status bar
        role_str = "Host 👑" if self.is_host else "Participant"
        self.status_bar_lbl.setText(
            f"  🟢 {self.my_name} ({role_str})  |  Room: {self.room_code}")

    def _toggle_mute(self):
        self.is_muted = self.mute_btn.isChecked()
        self.mute_btn.setText("🔇 Muted" if self.is_muted else "🎙 Mute")
        if self.rtc_worker.mic_track:
            self.rtc_worker.mic_track.is_muted = self.is_muted
        self.sio_worker.emit_event('toggle_mute', {'room_code': self.room_code})

    def _toggle_hand(self):
        self.hand_raised = self.hand_btn.isChecked()
        self.hand_btn.setText("✋ Hand Raised" if self.hand_raised else "✋ Raise Hand")
        self.sio_worker.emit_event('toggle_hand', {'room_code': self.room_code})

    def _request_token(self):
        self.sio_worker.emit_event('request_token', {'room_code': self.room_code})

    def _drop_token(self):
        self.sio_worker.emit_event('drop_token', {'room_code': self.room_code})

    def _clear_canvas(self):
        self.sio_worker.emit_event('clear_canvas', {'room_code': self.room_code})

    def _pick_color(self):
        c = QColorDialog.getColor(self.canvas.color, self, "Pick Color")
        if c.isValid():
            self.canvas.color = c

    def _toggle_eraser(self):
        """Toggle eraser mode on the canvas.

        When ON:
          - canvas.is_eraser_mode = True  → mouse events use ERASER_COLOR
            and ERASER_WIDTH automatically (see _effective_color/width).
          - The button label changes to give clear visual feedback.
          - The cursor changes to a crosshair so the user knows they are
            in a special mode.
        When OFF:
          - canvas.is_eraser_mode = False → normal color/width restored.
          - Cursor reverts to whatever _update_token_ui() normally sets.
        """
        self.canvas.is_eraser_mode = self.eraser_btn.isChecked()
        if self.canvas.is_eraser_mode:
            self.eraser_btn.setText("🧽 Eraser (ON)")
            # Use a blank cursor so the user can see the eraser area clearly
            self.canvas.setCursor(QCursor(Qt.CursorShape.BlankCursor))
        else:
            self.eraser_btn.setText("🧽 Eraser")
            # Restore the default cursor for the current token state
            self._update_token_ui()

    def _host_force_mute(self):
        sid = self.panel.selected_sid()
        if not sid or sid == self.my_sid:
            QMessageBox.warning(self, "Select Participant",
                                "Select a participant to force mute.")
            return
        self.sio_worker.emit_event('force_mute', {
            'room_code': self.room_code,
            'target_sid': sid,
        })

    def _host_ask_unmute(self):
        sid = self.panel.selected_sid()
        if not sid or sid == self.my_sid:
            QMessageBox.warning(self, "Select Participant",
                                "Select a participant to request unmute.")
            return
        self.sio_worker.emit_event('ask_unmute', {
            'room_code': self.room_code,
            'target_sid': sid,
        })

    def _update_token_ui(self):
        """
        Refresh all token-related UI controls based on current token ownership.

        This is called whenever the token changes hands. It enforces the
        single-writer constraint in the UI:
          - canvas.can_draw: only the token holder can trigger mouse events
          - drop_btn: only enabled when you hold the token
          - request_btn: only enabled when you DON'T hold the token
          - Cursor: CrossCursor (drawing) vs ArrowCursor (read-only)

        WHY centralise this in one method?
        Token state affects 5 different UI elements. Centralising ensures
        they always stay in sync — no risk of forgetting to update one.
        """
        i_have_token = (self.token_holder_sid == self.my_sid)
        self.canvas.can_draw = i_have_token
        self.drop_btn.setEnabled(i_have_token)
        self.request_btn.setEnabled(not i_have_token)

        if self.token_holder_sid is None:
            lbl = "Token: Open Floor"
        else:
            holder = self._participants.get(self.token_holder_sid, {})
            name = holder.get('name', '?')
            lbl = f"Token: {name}" + (" (You ✏️)" if i_have_token else "")
        self.token_lbl.setText(lbl)

        if i_have_token:
            self.canvas.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        else:
            self.canvas.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def closeEvent(self, event):
        """
        Qt calls this when the user closes the window.
        We must stop both background QThreads before accepting the close,
        otherwise Python may crash with 'QThread: Destroyed while thread
        is still running'. wait(3000) gives each thread 3 seconds to
        clean up its connections before we force-close.
        """
        if self.rtc_worker:
            self.rtc_worker.stop_worker()
        self.sio_worker.stop_worker()
        self.sio_worker.wait(3000)
        if self.rtc_worker:
            self.rtc_worker.wait(3000)
        event.accept()


# ── Entry Point ───────────────────────────────────────────────────
def main():
    """
    Application bootstrap sequence:
      1. Create QApplication (initialises Qt event loop infrastructure)
      2. Show LoginDialog (modal — blocks until user submits or cancels)
      3. Create MainWindow with the collected parameters
      4. Start Qt event loop (app.exec()) — blocks until window closes

    WHY Fusion style?
    Qt's default style is platform-native (looks like Windows/macOS controls).
    Fusion is a cross-platform style that gives consistent appearance and
    works well with custom dark-mode stylesheets.

    WHY wrap MainWindow creation in try/except?
    aiortc or sounddevice can raise on import if native libraries are
    missing. Catching here shows the user a readable error dialog instead
    of a crash, which is critical for a distributed .exe used by non-developers.
    """
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))

    dlg = LoginDialog()
    if dlg.exec() != QDialog.DialogCode.Accepted:
        sys.exit(0)

    name, room_code, server_url = dlg.get_values()
    try:
        win = MainWindow(name, room_code, server_url)
        win.show()
        sys.exit(app.exec())
    except Exception as e:
        import traceback
        QMessageBox.critical(
            None, "Startup Error",
            f"The app crashed on startup:\n\n"
            f"{type(e).__name__}: {e}\n\n"
            f"{traceback.format_exc()}"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
