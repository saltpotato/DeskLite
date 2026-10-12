import asyncio
import collections
import json
import logging
import struct
import threading
import time
import websockets

from PySide6.QtCore import Qt, QRectF, Signal, QObject
from PySide6.QtGui import QImage, QPainter, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QMessageBox, QSizePolicy
)

from common import get_clipboard_text, set_clipboard_text

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
    HAS_WEBRTC = True
except ImportError:
    HAS_WEBRTC = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [VIEWER] %(message)s")

QT_KEY_MAP = {
    Qt.Key.Key_Return: "return",
    Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Space: "space",
    Qt.Key.Key_Shift: "shift",
    Qt.Key.Key_Control: "ctrl",
    Qt.Key.Key_Alt: "alt",
    Qt.Key.Key_Escape: "escape",
    Qt.Key.Key_Delete: "delete",
    Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "page_up",
    Qt.Key.Key_PageDown: "page_down",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Right: "right",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Down: "down",
    Qt.Key.Key_CapsLock: "caps_lock",
    Qt.Key.Key_Meta: "win",
    Qt.Key.Key_Super_L: "win",
    Qt.Key.Key_Super_R: "win",
    Qt.Key.Key_F1: "f1",
    Qt.Key.Key_F2: "f2",
    Qt.Key.Key_F3: "f3",
    Qt.Key.Key_F4: "f4",
    Qt.Key.Key_F5: "f5",
    Qt.Key.Key_F6: "f6",
    Qt.Key.Key_F7: "f7",
    Qt.Key.Key_F8: "f8",
    Qt.Key.Key_F9: "f9",
    Qt.Key.Key_F10: "f10",
    Qt.Key.Key_F11: "f11",
    Qt.Key.Key_F12: "f12",
}


class VideoCanvas(QWidget):
    """Hardware/Retina-aware video surface that renders full-resolution QImage frames via QPainter."""
    def __init__(self, viewer):
        super().__init__(viewer)
        self.viewer = viewer
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.img_pos_x = 0.0
        self.img_pos_y = 0.0
        self.img_draw_w = 1280.0
        self.img_draw_h = 720.0

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        qimg = self.viewer.base_qimage
        if qimg is None or qimg.isNull():
            painter.end()
            return

        cw = max(1.0, float(self.width()))
        ch = max(1.0, float(self.height()))
        aspect = float(qimg.width()) / max(1.0, float(qimg.height()))

        if (cw / ch) > aspect:
            draw_h = ch
            draw_w = ch * aspect
        else:
            draw_w = cw
            draw_h = cw / aspect

        self.img_draw_w = draw_w
        self.img_draw_h = draw_h
        self.img_pos_x = (cw - draw_w) * 0.5
        self.img_pos_y = (ch - draw_h) * 0.5

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        target_rect = QRectF(self.img_pos_x, self.img_pos_y, draw_w, draw_h)
        painter.drawImage(target_rect, qimg)
        painter.end()

    def _calc_normalized_coords(self, pos):
        if self.img_draw_w <= 0 or self.img_draw_h <= 0:
            return None, None
        rel_x = pos.x() - self.img_pos_x
        rel_y = pos.y() - self.img_pos_y
        if 0.0 <= rel_x <= self.img_draw_w and 0.0 <= rel_y <= self.img_draw_h:
            nx = min(1.0, max(0.0, rel_x / self.img_draw_w))
            ny = min(1.0, max(0.0, rel_y / self.img_draw_h))
            return nx, ny
        return None, None

    def mouseMoveEvent(self, event):
        now = time.time()
        if now - self.viewer.last_mouse_time < 0.012:
            return
        self.viewer.last_mouse_time = now
        pos = event.position()
        nx, ny = self._calc_normalized_coords(pos)
        if nx is not None:
            self.viewer.send_input({"event": "mouse_move", "x": nx, "y": ny})

    def _button_name(self, qt_btn):
        if qt_btn == Qt.MouseButton.RightButton:
            return "right"
        if qt_btn == Qt.MouseButton.MiddleButton:
            return "middle"
        return "left"

    def mousePressEvent(self, event):
        self.setFocus()
        pos = event.position()
        nx, ny = self._calc_normalized_coords(pos)
        if nx is not None:
            self.viewer.send_input({"event": "mouse_move", "x": nx, "y": ny})
            self.viewer.send_input({"event": "mouse_down", "button": self._button_name(event.button())})

    def mouseReleaseEvent(self, event):
        pos = event.position()
        nx, ny = self._calc_normalized_coords(pos)
        if nx is not None:
            self.viewer.send_input({"event": "mouse_move", "x": nx, "y": ny})
            self.viewer.send_input({"event": "mouse_up", "button": self._button_name(event.button())})

    def wheelEvent(self, event):
        dy = event.angleDelta().y()
        if dy != 0:
            delta = 1 if dy > 0 else -1
            self.viewer.send_input({"event": "mouse_wheel", "delta": delta})

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return
        key_name = QT_KEY_MAP.get(event.key(), "")
        text = event.text()
        char = text if (text and len(text) == 1 and ord(text) >= 32) else ""
        if not key_name and char:
            key_name = char
        if key_name or char:
            self.viewer.send_input({"event": "key_down", "key": key_name, "char": char})

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        key_name = QT_KEY_MAP.get(event.key(), "")
        text = event.text()
        char = text if (text and len(text) == 1 and ord(text) >= 32) else ""
        if not key_name and char:
            key_name = char
        if key_name or char:
            self.viewer.send_input({"event": "key_up", "key": key_name, "char": char})

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.viewer._check_clipboard_sync()


class RemoteDesktopViewer(QWidget):
    frame_ready_sig = Signal()
    status_sig = Signal(str)
    error_sig = Signal(str)

    def __init__(self, master, relay_url, target_id, password=""):
        super().__init__(None, Qt.WindowType.Window)
        self.master_ref = master
        if master is not None and not master.windowIcon().isNull():
            self.setWindowIcon(master.windowIcon())
        self.relay_url = relay_url
        self.target_id = target_id
        self.password = password

        self.setWindowTitle(f"Remote Session: {target_id}")
        self.resize(1280, 760)
        self.setMinimumSize(640, 480)

        self.remote_width = 1920
        self.remote_height = 1080
        self.base_qimage = None

        self.render_pending = False
        self.running = True
        self.ws = None
        self.p2p_ws = None
        self.webrtc_pc = None
        self.webrtc_channel = None
        self.transport_mode = "Signaling Relay"
        self.p2p_upgrading = False
        self.dc_chunks = {}
        self.loop = None

        self.fps_count = 0
        self.fps_timer = time.time()
        self.fps_display = 0
        self.last_mouse_time = 0.0
        self.last_clipboard = ""
        self.frame_queue = collections.deque(maxlen=30)

        self.frame_ready_sig.connect(self._process_latest_frame)
        self.status_sig.connect(self._update_status_label)
        self.error_sig.connect(self._show_error)

        self._build_ui()
        self.show()
        self.canvas.setFocus()

        self.worker_thread = threading.Thread(target=self._run_async_worker, daemon=True)
        self.worker_thread.start()

    def _make_btn(self, text, bg_color, hover_color, callback):
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg_color};
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 4px 10px;
                font-size: 12px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background-color: {hover_color};
            }}
        """)
        btn.clicked.connect(callback)
        return btn

    def _build_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Top toolbar
        self.toolbar = QWidget(self)
        self.toolbar.setFixedHeight(38)
        self.toolbar.setStyleSheet("background-color: #1e293b; border-bottom: 1px solid #334155;")
        tb_layout = QHBoxLayout(self.toolbar)
        tb_layout.setContentsMargins(12, 4, 12, 4)
        tb_layout.setSpacing(6)

        self.status_label = QLabel("Connecting...")
        self.status_label.setStyleSheet("color: #f8fafc; font-size: 12px; font-weight: 500; border: none;")
        tb_layout.addWidget(self.status_label)
        tb_layout.addStretch(1)

        tb_layout.addWidget(self._make_btn("🚀 Fast", "#059669", "#10b981", lambda: self.set_quality(65, 1.0)))
        tb_layout.addWidget(self._make_btn("⚖️ Balanced", "#2563eb", "#3b82f6", lambda: self.set_quality(75, 1.0)))
        tb_layout.addWidget(self._make_btn("💎 HQ", "#7c3aed", "#8b5cf6", lambda: self.set_quality(85, 1.0)))

        tb_layout.addWidget(self._make_btn("Ctrl+Alt+Del", "#334155", "#475569", self.send_ctrl_alt_del))
        tb_layout.addWidget(self._make_btn("🪟 Start", "#334155", "#475569", lambda: self.send_shortcut("win_key")))
        tb_layout.addWidget(self._make_btn("📁 Explorer", "#334155", "#475569", lambda: self.send_shortcut("explorer")))
        tb_layout.addWidget(self._make_btn("📋 Paste", "#334155", "#475569", self.send_clipboard_to_host))
        tb_layout.addWidget(self._make_btn("🖥️ Desktop", "#334155", "#475569", lambda: self.send_shortcut("desktop")))

        root_layout.addWidget(self.toolbar)

        self.canvas = VideoCanvas(self)
        root_layout.addWidget(self.canvas, 1)

    def _update_status_label(self, text):
        self.status_label.setText(text)

    def send_shortcut(self, name):
        self.send_input({"event": "shortcut", "name": name})

    def send_clipboard_to_host(self):
        text = get_clipboard_text()
        if text and self.loop and self.loop.is_running():
            self.last_clipboard = text
            msg = json.dumps({"type": "clipboard", "text": text})
            asyncio.run_coroutine_threadsafe(self._ws_send(msg), self.loop)
            self.status_sig.emit("📋 Clipboard sent to host")

    def _check_clipboard_sync(self):
        cur = get_clipboard_text()
        if cur and cur != self.last_clipboard and len(cur) < 500000:
            self.send_clipboard_to_host()

    def send_ctrl_alt_del(self):
        self.send_input({"event": "key_down", "key": "ctrl"})
        self.send_input({"event": "key_down", "key": "alt"})
        self.send_input({"event": "key_down", "key": "delete"})
        self.send_input({"event": "key_up", "key": "delete"})
        self.send_input({"event": "key_up", "key": "alt"})
        self.send_input({"event": "key_up", "key": "ctrl"})

    def set_quality(self, quality, scale):
        if self.loop and self.loop.is_running():
            msg = json.dumps({"type": "set_quality", "quality": quality, "scale": scale})
            asyncio.run_coroutine_threadsafe(self._ws_send(msg), self.loop)
            self.status_sig.emit(f"Profile active: Quality {quality}%, Scale {int(scale*100)}%")

    def send_input(self, data):
        if self.loop and self.loop.is_running():
            msg = json.dumps({"type": "input", "data": data})
            asyncio.run_coroutine_threadsafe(self._ws_send(msg), self.loop)

    async def _ws_send(self, msg):
        """Routes outgoing control messages over Direct P2P first (0 KB/s on VPS), falling back to signaling."""
        try:
            if self.p2p_ws is not None:
                await self.p2p_ws.send(msg)
                return
            if self.webrtc_channel is not None and getattr(self.webrtc_channel, "readyState", "") == "open":
                self.webrtc_channel.send(msg)
                return
            if self.ws is not None:
                await self.ws.send(msg)
        except Exception:
            pass

    def _run_async_worker(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        while self.running:
            try:
                self.loop.run_until_complete(self._connect_and_view())
            except Exception as e:
                logging.debug(f"Connection error: {e}")
            if self.running:
                self.status_sig.emit("🟡 Connection lost – reconnecting in 2s...")
                time.sleep(2)

    def _handle_incoming_binary(self, message):
        """Processes incoming binary frame (or WebRTC DataChannel chunk) and schedules render."""
        if len(message) < 4:
            return
        if message[:2] == b"DC" and len(message) > 6:
            frame_seq, idx, total = struct.unpack(">HBB", message[2:6])
            bucket = self.dc_chunks.get(frame_seq)
            if bucket is None or bucket["total"] != total:
                if len(self.dc_chunks) > 4:
                    self.dc_chunks.clear()
                bucket = {"total": total, "parts": {}}
                self.dc_chunks[frame_seq] = bucket
            bucket["parts"][idx] = message[6:]
            if len(bucket["parts"]) == total:
                full_bytes = b"".join(bucket["parts"][i] for i in range(total))
                self.dc_chunks.pop(frame_seq, None)
                self._enqueue_frame(full_bytes)
            return

        self._enqueue_frame(message)

    def _enqueue_frame(self, frame_bytes):
        if frame_bytes[:2] != b"DL":
            self.frame_queue.clear()
        self.frame_queue.append(frame_bytes)
        if not self.render_pending:
            self.render_pending = True
            self.frame_ready_sig.emit()

    def _handle_incoming_json(self, message, from_signaling=False):
        try:
            meta = json.loads(message)
            msg_type = meta.get("type")
            if msg_type == "screen_info":
                self.remote_width = meta.get("width", 1920)
                self.remote_height = meta.get("height", 1080)
                if from_signaling and not self.p2p_upgrading and self.p2p_ws is None and self.webrtc_channel is None:
                    endpoints = meta.get("p2p_endpoints", [])
                    token = meta.get("p2p_token", "")
                    webrtc_ok = meta.get("webrtc_supported", False)
                    self.p2p_upgrading = True
                    asyncio.create_task(self._upgrade_to_p2p(endpoints, token, webrtc_ok))
            elif msg_type == "webrtc_answer" and self.webrtc_pc is not None:
                asyncio.create_task(self._apply_webrtc_answer(meta))
            elif msg_type == "host_disconnected":
                self.status_sig.emit("🟡 Host disconnected – waiting for reconnect...")
                return False
            elif msg_type == "clipboard":
                text = meta.get("text", "")
                if text and text != self.last_clipboard:
                    self.last_clipboard = text
                    set_clipboard_text(text)
                    self.status_sig.emit("📋 Clipboard synced from host")
        except Exception:
            pass
        return True

    async def _upgrade_to_p2p(self, endpoints, token, webrtc_ok):
        """Upgrades the session from VPS signaling to a Direct P2P connection (LAN/VPN WebSocket or WebRTC STUN)."""
        if endpoints and token:
            for ep in endpoints:
                if not self.running or self.p2p_ws is not None:
                    break
                try:
                    p2p_sock = await asyncio.wait_for(
                        websockets.connect(ep, max_size=20 * 1024 * 1024, ping_interval=20, ping_timeout=40),
                        timeout=0.75
                    )
                    await p2p_sock.send(json.dumps({"action": "p2p_auth", "token": token}))
                    resp_raw = await asyncio.wait_for(p2p_sock.recv(), timeout=1.5)
                    resp = json.loads(resp_raw)
                    if resp.get("status") == "p2p_connected":
                        self.p2p_ws = p2p_sock
                        self.transport_mode = "🟢 Direct P2P (0 KB/s on VPS)"
                        logging.info(f"Upgraded to Direct P2P WebSocket via {ep}!")
                        if self.ws is not None:
                            try:
                                await self.ws.send(json.dumps({"type": "p2p_takeover"}))
                            except Exception:
                                pass
                        self.status_sig.emit(f"🟢 Direct P2P Connected to {self.target_id} (0 KB/s on VPS)")
                        asyncio.create_task(self._read_direct_p2p_ws(p2p_sock))
                        return
                    else:
                        await p2p_sock.close()
                except Exception:
                    continue

        if HAS_WEBRTC and webrtc_ok and self.ws is not None and self.running:
            try:
                config = RTCConfiguration(iceServers=[
                    RTCIceServer(urls=["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"])
                ])
                pc = RTCPeerConnection(configuration=config)
                self.webrtc_pc = pc
                channel = pc.createDataChannel("desklite")

                @channel.on("open")
                def on_open():
                    self.webrtc_channel = channel
                    self.transport_mode = "🟢 Direct P2P WebRTC (0 KB/s on VPS)"
                    logging.info("Upgraded to Direct P2P WebRTC DataChannel!")
                    if self.ws is not None:
                        asyncio.ensure_future(self.ws.send(json.dumps({"type": "p2p_takeover"})))
                    self.status_sig.emit(f"🟢 Direct P2P WebRTC Connected to {self.target_id} (0 KB/s on VPS)")

                @channel.on("message")
                def on_message(msg):
                    if isinstance(msg, bytes):
                        self._handle_incoming_binary(msg)
                    elif isinstance(msg, str):
                        self._handle_incoming_json(msg, from_signaling=False)

                @channel.on("close")
                def on_close():
                    if self.webrtc_channel == channel:
                        self.webrtc_channel = None
                        self.transport_mode = "Signaling Relay"

                offer = await pc.createOffer()
                await pc.setLocalDescription(offer)
                await self.ws.send(json.dumps({
                    "type": "webrtc_offer",
                    "sdp": pc.localDescription.sdp,
                    "sdpType": pc.localDescription.type
                }))
            except Exception as e:
                logging.debug(f"WebRTC P2P offer failed: {e}")

    async def _apply_webrtc_answer(self, meta):
        try:
            if self.webrtc_pc is not None:
                ans = RTCSessionDescription(sdp=meta["sdp"], type=meta.get("sdpType", "answer"))
                await self.webrtc_pc.setRemoteDescription(ans)
        except Exception as e:
            logging.debug(f"Failed to apply WebRTC answer: {e}")

    async def _read_direct_p2p_ws(self, p2p_sock):
        try:
            async for message in p2p_sock:
                if not self.running:
                    break
                if isinstance(message, bytes):
                    self._handle_incoming_binary(message)
                elif isinstance(message, str):
                    self._handle_incoming_json(message, from_signaling=False)
        except Exception as e:
            logging.debug(f"Direct P2P socket closed: {e}")
        finally:
            if self.p2p_ws == p2p_sock:
                self.p2p_ws = None
                self.transport_mode = "Signaling Relay"

    async def _connect_and_view(self):
        self.p2p_ws = None
        self.webrtc_channel = None
        self.p2p_upgrading = False
        self.transport_mode = "Signaling Relay"
        try:
            async with websockets.connect(
                self.relay_url,
                max_size=20 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=40
            ) as ws:
                self.ws = ws
                req = {
                    "action": "connect",
                    "target_id": self.target_id,
                    "password": self.password
                }
                await ws.send(json.dumps(req))

                resp_str = await ws.recv()
                resp = json.loads(resp_str)
                if resp.get("status") != "connected":
                    err_msg = resp.get("message", "Connection refused")
                    if "password" in err_msg.lower() or "passwort" in err_msg.lower():
                        self.running = False
                        self.error_sig.emit(err_msg)
                        return
                    return

                self.status_sig.emit(f"Connected to {self.target_id} (Negotiating Direct P2P...)")

                async for message in ws:
                    if not self.running:
                        break
                    if isinstance(message, bytes):
                        self._handle_incoming_binary(message)
                    elif isinstance(message, str):
                        if not self._handle_incoming_json(message, from_signaling=True):
                            break
        except Exception:
            pass
        finally:
            if self.p2p_ws is not None:
                try:
                    await self.p2p_ws.close()
                except Exception:
                    pass
                self.p2p_ws = None
            if self.webrtc_pc is not None:
                try:
                    await self.webrtc_pc.close()
                except Exception:
                    pass
                self.webrtc_pc = None
                self.webrtc_channel = None

    def _process_latest_frame(self):
        self.render_pending = False
        if not self.running or not self.frame_queue:
            return

        try:
            while self.frame_queue:
                frame_bytes = self.frame_queue.popleft()
                if frame_bytes[:2] == b"DL" and len(frame_bytes) > 10:
                    if self.base_qimage is not None and not self.base_qimage.isNull():
                        dx, dy, dw, dh = struct.unpack(">HHHH", frame_bytes[2:10])
                        patch = QImage.fromData(frame_bytes[10:])
                        if not patch.isNull():
                            p = QPainter(self.base_qimage)
                            p.drawImage(dx, dy, patch)
                            p.end()
                else:
                    qimg = QImage.fromData(frame_bytes)
                    if not qimg.isNull():
                        self.base_qimage = qimg

            self.canvas.update()

            self.fps_count += 1
            now = time.time()
            if now - self.fps_timer >= 1.0:
                self.fps_display = self.fps_count
                self.fps_count = 0
                self.fps_timer = now
                fps_text = f"{self.transport_mode} | {self.target_id} | {self.remote_width}x{self.remote_height} | {self.fps_display} FPS"
                self.status_label.setText(fps_text)

        except Exception as e:
            logging.debug(f"Frame decode error: {e}")

    def _show_error(self, message):
        QMessageBox.critical(self, "Connection Error", message)
        self.close()

    async def _shutdown_connections(self):
        if self.p2p_ws is not None:
            try:
                await self.p2p_ws.close()
            except Exception:
                pass
            self.p2p_ws = None
        if self.webrtc_channel is not None:
            try:
                self.webrtc_channel.close()
            except Exception:
                pass
            self.webrtc_channel = None
        if self.webrtc_pc is not None:
            try:
                await self.webrtc_pc.close()
            except Exception:
                pass
            self.webrtc_pc = None
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    def closeEvent(self, event):
        self.running = False
        if self.loop and self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown_connections(), self.loop)
            except Exception:
                pass
        super().closeEvent(event)
        if self.master_ref is not None and hasattr(self.master_ref, "active_viewers"):
            try:
                if self in self.master_ref.active_viewers:
                    self.master_ref.active_viewers.remove(self)
                remaining = [v for v in self.master_ref.active_viewers if v is not self and v.isVisible()]
                if not remaining and not self.master_ref.isVisible():
                    if hasattr(self.master_ref, "shutdown_and_exit"):
                        from PySide6.QtCore import QTimer
                        QTimer.singleShot(150, self.master_ref.shutdown_and_exit)
            except Exception:
                pass


