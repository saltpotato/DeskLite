"""Local emulation mode infrastructure (Server = Client on 127.0.0.1).

Provides:
- `LocalRelayServer`: In-process WebSocket + HTTP relay serving both native Qt
  and browser Web Viewers on `127.0.0.1` without touching external VPS or `config.json`.
- `VirtualDesktopScene`: Interactive offscreen test desktop (draggable window,
  text editor, scrollable list, 72x72 spinner, semi-transparent fade modal,
  toggleable popup window, and optional Auto-Stress sequence) rendered as BGRA frames.
- `exclude_window_from_capture`: Applies Windows `WDA_EXCLUDEFROMCAPTURE` so
  real-desktop loopback capture ignores the local Viewer window (no infinity mirror).
"""

import asyncio
import ctypes
import json
import logging
import math
import os
import pathlib
import sys
import threading
import time

import numpy as np
import websockets
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPen
from websockets.datastructures import Headers
from websockets.http11 import Response


WDA_EXCLUDEFROMCAPTURE = 0x00000011


def exclude_window_from_capture(widget) -> bool:
    """Prevent Windows DXGI/GDI screen capture from seeing `widget` (anti-mirror)."""
    if sys.platform != "win32" or widget is None:
        return False
    try:
        hwnd = int(widget.winId())
        ok = bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
        if ok:
            logging.info("[EMULATION] Applied WDA_EXCLUDEFROMCAPTURE to local viewer window.")
        else:
            logging.warning("[EMULATION] SetWindowDisplayAffinity returned False (Windows 10 2004+ required).")
        return ok
    except Exception as e:
        logging.warning(f"[EMULATION] Could not exclude viewer window from capture: {e}")
        return False


def _resolve_web_file() -> pathlib.Path:
    """Locate `server/web/index.html` cleanly in source and PyInstaller runs."""
    candidates = [
        pathlib.Path(__file__).resolve().parent / "server" / "web" / "index.html",
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(pathlib.Path(meipass) / "server" / "web" / "index.html")
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


class LocalRelayServer:
    """Lightweight embedded WebSocket + HTTP DeskLite relay bound strictly to 127.0.0.1."""

    def __init__(self, preferred_ports=(18765, 18766, 0)):
        self.preferred_ports = preferred_ports
        self.port = None
        self.hosts = {}
        self.viewers = {}
        self.peer_auth = {}
        self.running = False
        self._loop = None
        self._server = None
        self._thread = None
        self._ready_event = threading.Event()
        self._start_error = None
        self._web_file = _resolve_web_file()

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _process_http_request(self, connection, request):
        upgrade = request.headers.get("Upgrade", "")
        if upgrade and upgrade.lower() == "websocket":
            return None

        path = request.path
        if path == "/" or path.startswith("/?") or path == "/index.html":
            if self._web_file.exists():
                body = self._web_file.read_bytes()
            else:
                body = b"<h1>DeskLite Local Emulation Relay</h1><p>Use the native Qt Viewer or place server/web/index.html.</p>"
            return Response(
                status_code=200,
                reason_phrase="OK",
                headers=Headers([
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Length", str(len(body))),
                ]),
                body=body,
            )
        return None

    async def _handle_websocket(self, websocket):
        peer_id = None
        role = None
        try:
            first_msg = await websocket.recv()
            data = json.loads(first_msg)
            action = data.get("action")
            raw_id = str(data.get("id", "")).replace(" ", "").strip()
            pwd = data.get("password", "").strip()

            if action == "register":
                role = "host"
                peer_id = raw_id
                if not peer_id:
                    await websocket.send(json.dumps({"status": "error", "message": "ID required"}))
                    return

                self.hosts[peer_id] = websocket
                self.peer_auth[peer_id] = pwd
                await websocket.send(json.dumps({"status": "registered", "id": peer_id}))

                async for message in websocket:
                    if peer_id in self.viewers:
                        dead = []
                        for v_ws in list(self.viewers[peer_id]):
                            try:
                                await v_ws.send(message)
                            except Exception:
                                dead.append(v_ws)
                        for d in dead:
                            if d in self.viewers.get(peer_id, []):
                                self.viewers[peer_id].remove(d)

            elif action == "connect":
                role = "viewer"
                target_id = str(data.get("target_id", "")).replace(" ", "").strip()
                if target_id not in self.hosts:
                    await websocket.send(json.dumps({"status": "error", "message": f"Host '{target_id}' is not online."}))
                    return

                expected_pwd = self.peer_auth.get(target_id, "")
                if expected_pwd and pwd != expected_pwd:
                    await websocket.send(json.dumps({"status": "error", "message": "Invalid password."}))
                    return

                self.viewers.setdefault(target_id, []).append(websocket)
                host_ws = self.hosts[target_id]
                await websocket.send(json.dumps({"status": "connected", "target_id": target_id}))

                try:
                    await host_ws.send(json.dumps({"type": "viewer_connected"}))
                except Exception:
                    pass

                async for message in websocket:
                    if target_id in self.hosts:
                        try:
                            await self.hosts[target_id].send(message)
                        except Exception:
                            break
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logging.debug(f"[EMU-RELAY] Session note ({role}/{peer_id}): {e}")
        finally:
            if role == "host" and peer_id in self.hosts and self.hosts[peer_id] == websocket:
                del self.hosts[peer_id]
                self.peer_auth.pop(peer_id, None)
                for v_ws in list(self.viewers.get(peer_id, [])):
                    try:
                        await v_ws.send(json.dumps({"type": "host_disconnected"}))
                    except Exception:
                        pass
            elif role == "viewer":
                for t_id, v_list in list(self.viewers.items()):
                    if websocket in v_list:
                        v_list.remove(websocket)
                        if t_id in self.hosts:
                            try:
                                await self.hosts[t_id].send(json.dumps({
                                    "type": "viewer_disconnected",
                                    "remaining_viewers": len(v_list),
                                }))
                            except Exception:
                                pass

    async def _serve(self):
        last_err = None
        for p in self.preferred_ports:
            try:
                self._server = await websockets.serve(
                    self._handle_websocket,
                    "127.0.0.1",
                    p,
                    process_request=self._process_http_request,
                    max_size=25 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=40,
                )
                sock = next(iter(self._server.sockets), None)
                self.port = sock.getsockname()[1] if sock else p
                break
            except Exception as exc:
                last_err = exc
        if self.port is None:
            self._start_error = last_err or RuntimeError("Failed to bind local relay server")
            self._ready_event.set()
            return

        logging.info(f"[EMULATION] Local loopback relay ready on ws://127.0.0.1:{self.port}")
        self._ready_event.set()
        while self.running:
            await asyncio.sleep(0.2)

        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    def start(self, timeout: float = 4.0) -> int:
        if self.running and self.port is not None:
            return self.port
        self.running = True
        self._ready_event.clear()
        self._start_error = None
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()
        if not self._ready_event.wait(timeout=timeout):
            raise TimeoutError("Timed out waiting for LocalRelayServer to bind")
        if self._start_error is not None:
            raise self._start_error
        return self.port

    def stop(self):
        self.running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)


class VirtualDesktopScene:
    """Interactive offscreen test desktop for isolated local Vision/Protocol testing.

    All input stays sandboxed inside this virtual desktop so the user's real cursor,
    keyboard, and windows are never touched.
    """

    def __init__(self, width: int = 1280, height: int = 720):
        self.width = width
        self.height = height
        self._lock = threading.Lock()
        self._dirty = True

        # Draggable main window geometry
        self.win_x = 120
        self.win_y = 75
        self.win_w = 720
        self.win_h = 520
        self.dragging_win = False
        self.drag_off_x = 0
        self.drag_off_y = 0

        # Secondary popup window (overlapping desktop to test BackgroundRestoreRecognizer)
        self.popup_open = False
        self.popup_rect = (880, 120, 320, 240)

        # Semi-transparent fade modal state
        self.fade_active = False
        self.fade_start = 0.0
        self.fade_duration = 1.6
        self._last_fade_alpha = 0

        # Spinner animation (72x72 HiDPI-sized spinner for AnimationCycleRecognizer)
        self.spinner_enabled = True
        self.spinner_angle = 0.0
        self._last_spin_step = -1

        # Text editor state
        self.editor_focused = True
        self.text_lines = [
            "DeskLite Emulation Sandbox (1280x720)",
            "Tippe hier Text, ziehe die Titelleiste, oder scrolle unten!",
            "Testzeile: IIII llll |||| ClearType check",
        ]
        self._last_blink_on = True

        # Scrollable log/list panel state
        self.scroll_rows = [
            f"[{i:02d}] Vision packet test row — Scroll2D & Slider check (val={i * 17 % 97:02d})"
            for i in range(1, 37)
        ]
        self.scroll_offset = 0
        self.max_scroll = max(0, len(self.scroll_rows) * 28 - 170)

        # Hover + cursor + auto-test state
        self.cursor_x = width // 2
        self.cursor_y = height // 2
        self.hovered_btn = None
        self.auto_stress = False
        self._auto_phase = 0.0
        self._last_auto_tick = 0.0

        # Pre-rendered static desktop backdrop (wallpaper + icons)
        self._backdrop = self._build_backdrop()

    def mark_dirty(self):
        with self._lock:
            self._dirty = True

    def _build_backdrop(self) -> QImage:
        img = QImage(self.width, self.height, QImage.Format.Format_ARGB32)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        grad = QLinearGradient(0, 0, self.width, self.height)
        grad.setColorAt(0.0, QColor("#0f172a"))
        grad.setColorAt(0.5, QColor("#1e293b"))
        grad.setColorAt(1.0, QColor("#0b1120"))
        p.fillRect(0, 0, self.width, self.height, grad)

        # Subtle background grid so BackgroundRestore & WindowMove have crisp texture
        p.setPen(QPen(QColor(148, 163, 184, 22), 1))
        for gx in range(0, self.width, 40):
            p.drawLine(gx, 0, gx, self.height)
        for gy in range(0, self.height, 40):
            p.drawLine(0, gy, self.width, gy)

        # Desktop icons on the left
        icons = [
            ("Explorer", "#38bdf8"),
            ("Terminal", "#10b981"),
            ("Vision Log", "#a855f7"),
            ("Settings", "#f59e0b"),
        ]
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        for idx, (label, col) in enumerate(icons):
            ix, iy = 28, 32 + idx * 96
            p.setPen(QPen(QColor(255, 255, 255, 40), 1))
            p.setBrush(QColor(col))
            p.drawRoundedRect(ix + 10, iy, 44, 44, 10, 10)
            p.setPen(QColor("#e2e8f0"))
            p.drawText(QRectF(ix - 4, iy + 50, 72, 22), Qt.AlignmentFlag.AlignCenter, label)

        # Bottom taskbar
        p.fillRect(0, self.height - 40, self.width, 40, QColor(15, 23, 42, 230))
        p.setPen(QColor(56, 189, 248, 140))
        p.drawLine(0, self.height - 40, self.width, self.height - 40)
        p.setPen(QColor("#cbd5e1"))
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        p.drawText(18, self.height - 15, "DeskLite Virtual Sandbox  •  Server = Client Emulation")
        p.end()
        return img

    def _button_rects(self):
        """Compute button hitboxes relative to current window position."""
        bx = self.win_x + 16
        by = self.win_y + 48
        bw = 122
        bh = 30
        gap = 10
        return {
            "fade": (bx, by, bw, bh),
            "popup": (bx + (bw + gap) * 1, by, bw, bh),
            "spinner": (bx + (bw + gap) * 2, by, bw, bh),
            "auto": (bx + (bw + gap) * 3, by, bw, bh),
        }

    def _editor_rect(self):
        return (self.win_x + 16, self.win_y + 92, self.win_w - 130, 128)

    def _spinner_rect(self):
        return (self.win_x + self.win_w - 102, self.win_y + 108, 72, 72)

    def _scroll_rect(self):
        return (self.win_x + 16, self.win_y + 236, self.win_w - 32, self.win_h - 252)

    def _titlebar_rect(self):
        return (self.win_x, self.win_y, self.win_w, 36)

    @staticmethod
    def _pt_in(x: int, y: int, rect: tuple) -> bool:
        rx, ry, rw, rh = rect
        return rx <= x <= rx + rw and ry <= y <= ry + rh

    def handle_event(self, evt: dict):
        """Process viewer input events strictly inside the virtual scene."""
        etype = evt.get("event")
        with self._lock:
            if etype == "mouse_move":
                nx = float(evt.get("x", 0.0))
                ny = float(evt.get("y", 0.0))
                px = max(0, min(self.width - 1, int(round(nx * self.width))))
                py = max(0, min(self.height - 1, int(round(ny * self.height))))
                self.cursor_x, self.cursor_y = px, py

                if self.dragging_win:
                    new_x = max(0, min(self.width - self.win_w, px - self.drag_off_x))
                    new_y = max(0, min(self.height - self.win_h - 40, py - self.drag_off_y))
                    if new_x != self.win_x or new_y != self.win_y:
                        self.win_x, self.win_y = new_x, new_y
                        self._dirty = True

                old_hover = self.hovered_btn
                self.hovered_btn = None
                for bname, brect in self._button_rects().items():
                    if self._pt_in(px, py, brect):
                        self.hovered_btn = bname
                        break
                if self.hovered_btn != old_hover:
                    self._dirty = True

            elif etype == "mouse_down" and evt.get("button", "left") == "left":
                px, py = self.cursor_x, self.cursor_y
                if self._pt_in(px, py, self._titlebar_rect()):
                    self.dragging_win = True
                    self.drag_off_x = px - self.win_x
                    self.drag_off_y = py - self.win_y
                    self._dirty = True
                    return

                for bname, brect in self._button_rects().items():
                    if self._pt_in(px, py, brect):
                        if bname == "fade":
                            self.fade_active = True
                            self.fade_start = time.time()
                        elif bname == "popup":
                            self.popup_open = not self.popup_open
                        elif bname == "spinner":
                            self.spinner_enabled = not self.spinner_enabled
                        elif bname == "auto":
                            self.auto_stress = not self.auto_stress
                            self._last_auto_tick = time.time()
                        self._dirty = True
                        return

                self.editor_focused = self._pt_in(px, py, self._editor_rect())
                self._dirty = True

            elif etype == "mouse_up":
                if self.dragging_win:
                    self.dragging_win = False
                    self._dirty = True

            elif etype == "mouse_wheel":
                delta = int(evt.get("delta", 0))
                if delta != 0:
                    step = -36 if delta > 0 else 36
                    new_off = max(0, min(self.max_scroll, self.scroll_offset + step))
                    if new_off != self.scroll_offset:
                        self.scroll_offset = new_off
                        self._dirty = True

            elif etype == "key_down" and self.editor_focused:
                key = str(evt.get("key", "")).lower()
                ch = evt.get("char", "")
                if key == "backspace":
                    if self.text_lines[-1]:
                        self.text_lines[-1] = self.text_lines[-1][:-1]
                    elif len(self.text_lines) > 1:
                        self.text_lines.pop()
                    self._dirty = True
                elif key in ("enter", "return"):
                    self.text_lines.append("")
                    if len(self.text_lines) > 6:
                        self.text_lines = self.text_lines[-6:]
                    self._dirty = True
                elif key == "space":
                    self.text_lines[-1] = (self.text_lines[-1] + " ")[:68]
                    self._dirty = True
                elif ch and len(ch) == 1 and ord(ch) >= 32:
                    self.text_lines[-1] = (self.text_lines[-1] + ch)[:68]
                    self._dirty = True

    def _compute_fade_alpha(self, now: float) -> int:
        if not self.fade_active:
            return 0
        t = (now - self.fade_start) / max(0.2, self.fade_duration)
        if t >= 1.0:
            self.fade_active = False
            return 0
        # Smooth triangle/sine envelope: fade in -> brief hold -> fade out
        return int(round(215 * math.sin(math.pi * t)))

    def _step_auto_stress(self, now: float):
        if not self.auto_stress:
            return
        if now - self._last_auto_tick < 0.04:
            return
        dt = min(0.15, now - self._last_auto_tick)
        self._last_auto_tick = now
        self._auto_phase += dt
        phase = self._auto_phase % 8.0

        if phase < 2.0:
            # 1) Smooth window drag + return (WindowMove2D + BackgroundRestore)
            prog = phase / 2.0
            self.win_x = 120 + int(90 * math.sin(math.pi * prog))
            self.win_y = 75 + int(45 * math.sin(math.pi * prog))
        elif phase < 4.0:
            # 2) Smooth list scroll down & up (Scroll2DAndSliderRecognizer)
            prog = (phase - 2.0) / 2.0
            self.scroll_offset = int(self.max_scroll * 0.5 * (1.0 - math.cos(2 * math.pi * prog)))
        elif phase < 5.8:
            # 3) Semi-transparent fade popup
            if not self.fade_active:
                self.fade_active = True
                self.fade_start = now
        elif phase < 7.2:
            # 4) Solid desktop popup toggle (BackgroundRestore on close)
            want_open = phase < 6.5
            if self.popup_open != want_open:
                self.popup_open = want_open
        else:
            # 5) Reset cleanly
            self.popup_open = False
            self.win_x, self.win_y = 120, 75
        self._dirty = True

    def render_frame(self, force: bool = False):
        """Render and return `(H, W, 4)` BGRA numpy array if visually dirty, else None."""
        now = time.time()
        with self._lock:
            self._step_auto_stress(now)

            fade_alpha = self._compute_fade_alpha(now)
            if fade_alpha != self._last_fade_alpha:
                self._last_fade_alpha = fade_alpha
                self._dirty = True

            blink_on = int(now * 1.8) % 2 == 0
            if self.editor_focused and blink_on != self._last_blink_on:
                self._last_blink_on = blink_on
                self._dirty = True

            if self.spinner_enabled:
                spin_step = int(now * 18) % 12
                if spin_step != self._last_spin_step:
                    self._last_spin_step = spin_step
                    self.spinner_angle = spin_step * 30.0
                    self._dirty = True

            if not force and not self._dirty:
                return None
            self._dirty = False

            img = self._backdrop.copy()
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            # Main window shadow & body
            p.fillRect(self.win_x + 6, self.win_y + 8, self.win_w, self.win_h, QColor(0, 0, 0, 95))
            p.setPen(QPen(QColor("#475569"), 1.5))
            p.setBrush(QColor("#f8fafc"))
            p.drawRoundedRect(self.win_x, self.win_y, self.win_w, self.win_h, 10, 10)

            # Titlebar
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#1e293b"))
            p.drawRoundedRect(self.win_x, self.win_y, self.win_w, 36, 10, 10)
            p.fillRect(self.win_x, self.win_y + 24, self.win_w, 12, QColor("#1e293b"))
            p.setPen(QColor("#f1f5f9"))
            p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            p.drawText(self.win_x + 14, self.win_y + 23, "DeskLite Emulation Window  (Titelleiste ziehen zum Testen)")

            # Action buttons
            btn_specs = [
                ("fade", "Fade Modal", "#7c3aed" if self.fade_active else "#2563eb"),
                ("popup", "Popup: AN" if self.popup_open else "Popup: AUS", "#059669" if self.popup_open else "#334155"),
                ("spinner", "Spinner: AN" if self.spinner_enabled else "Spinner: AUS", "#0284c7" if self.spinner_enabled else "#475569"),
                ("auto", "Auto-Stress: AN" if self.auto_stress else "Auto-Stress", "#d97706" if self.auto_stress else "#475569"),
            ]
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
            rects = self._button_rects()
            for key, label, base_hex in btn_specs:
                bx, by, bw, bh = rects[key]
                col = QColor(base_hex)
                if self.hovered_btn == key:
                    col = col.lighter(122)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
                p.drawRoundedRect(bx, by, bw, bh, 6, 6)
                p.setPen(QColor("#ffffff"))
                p.drawText(QRectF(bx, by, bw, bh), Qt.AlignmentFlag.AlignCenter, label)

            # Text editor box
            ex, ey, ew, eh = self._editor_rect()
            p.setPen(QPen(QColor("#2563eb" if self.editor_focused else "#cbd5e1"), 2 if self.editor_focused else 1))
            p.setBrush(QColor("#ffffff"))
            p.drawRoundedRect(ex, ey, ew, eh, 6, 6)
            p.setFont(QFont("Consolas", 11))
            p.setPen(QColor("#0f172a"))
            line_h = 20
            visible_lines = self.text_lines[-5:]
            for idx, line in enumerate(visible_lines):
                p.drawText(ex + 10, ey + 24 + idx * line_h, line)
            if self.editor_focused and blink_on:
                last_line = visible_lines[-1] if visible_lines else ""
                fm = p.fontMetrics()
                cx = ex + 10 + fm.horizontalAdvance(last_line)
                cy = ey + 8 + (max(0, len(visible_lines) - 1)) * line_h
                if cx < ex + ew - 8:
                    p.fillRect(cx, cy, 2, 16, QColor("#2563eb"))

            # 72x72 HiDPI Spinner Card
            sx, sy, sw, sh = self._spinner_rect()
            p.setPen(QPen(QColor("#cbd5e1"), 1))
            p.setBrush(QColor("#f1f5f9"))
            p.drawRoundedRect(sx, sy, sw, sh, 10, 10)
            cx, cy = sx + sw / 2.0, sy + sh / 2.0
            for dot in range(12):
                ang = math.radians(self.spinner_angle + dot * 30.0)
                dx = cx + 22.0 * math.cos(ang)
                dy = cy + 22.0 * math.sin(ang)
                alpha = int(35 + (dot / 11.0) * 220) if self.spinner_enabled else 70
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(37, 99, 235, alpha))
                p.drawEllipse(QRectF(dx - 4.0, dy - 4.0, 8.0, 8.0))

            # Scrollable list + scrollbar
            lx, ly, lw, lh = self._scroll_rect()
            p.setPen(QPen(QColor("#cbd5e1"), 1))
            p.setBrush(QColor("#ffffff"))
            p.drawRoundedRect(lx, ly, lw, lh, 6, 6)
            p.save()
            p.setClipRect(lx + 2, ly + 2, lw - 18, lh - 4)
            p.setFont(QFont("Consolas", 10))
            row_h = 28
            for idx, text in enumerate(self.scroll_rows):
                ry = ly + 6 + idx * row_h - self.scroll_offset
                if ry + row_h < ly or ry > ly + lh:
                    continue
                if idx % 2 == 0:
                    p.fillRect(lx + 4, ry, lw - 24, row_h - 2, QColor("#f8fafc"))
                p.setPen(QColor("#1e293b"))
                p.drawText(lx + 12, ry + 19, text)
            p.restore()

            # Scrollbar track & thumb
            sb_x = lx + lw - 14
            p.fillRect(sb_x, ly + 4, 10, lh - 8, QColor("#e2e8f0"))
            total_h = max(lh, len(self.scroll_rows) * row_h)
            thumb_h = max(28, int((lh - 8) * (lh / total_h)))
            ratio = (self.scroll_offset / self.max_scroll) if self.max_scroll > 0 else 0.0
            thumb_y = ly + 4 + int((lh - 8 - thumb_h) * ratio)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#64748b"))
            p.drawRoundedRect(sb_x + 1, thumb_y, 8, thumb_h, 4, 4)

            # Secondary floating popup over desktop (BackgroundRestore target)
            if self.popup_open:
                px, py, pw, ph = self.popup_rect
                p.fillRect(px + 6, py + 8, pw, ph, QColor(0, 0, 0, 100))
                p.setPen(QPen(QColor("#10b981"), 2))
                p.setBrush(QColor("#0f172a"))
                p.drawRoundedRect(px, py, pw, ph, 10, 10)
                p.setPen(QColor("#34d399"))
                p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
                p.drawText(px + 18, py + 34, "Desktop Popup Overlay")
                p.setPen(QColor("#cbd5e1"))
                p.setFont(QFont("Segoe UI", 9))
                p.drawText(QRectF(px + 18, py + 52, pw - 36, ph - 70),
                           Qt.TextFlag.TextWordWrap,
                           "Schließe dieses Popup erneut, um BackgroundRestoreRecognizer "
                           "(0 KB Wiederherstellung des Desktop-Hintergrunds) zu testen.")

            # Semi-transparent Fade Modal (tests Windows-style alpha fade)
            if fade_alpha > 0:
                mw, mh = 380, 170
                mx = self.win_x + (self.win_w - mw) // 2
                my = self.win_y + (self.win_h - mh) // 2
                p.setPen(QPen(QColor(56, 189, 248, fade_alpha), 2))
                p.setBrush(QColor(15, 23, 42, fade_alpha))
                p.drawRoundedRect(mx, my, mw, mh, 12, 12)
                p.setPen(QColor(248, 250, 252, fade_alpha))
                p.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
                p.drawText(QRectF(mx, my + 24, mw, 36), Qt.AlignmentFlag.AlignCenter, "Halbtransparentes Fade-Fenster")
                p.setFont(QFont("Segoe UI", 10))
                p.drawText(QRectF(mx + 20, my + 70, mw - 40, 70),
                           Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                           f"Alpha-Opazität: {int(fade_alpha * 100 / 255)}%")

            p.end()

            raw = np.frombuffer(img.bits(), np.uint8).reshape(self.height, img.bytesPerLine())
            return raw[:, : self.width * 4].reshape(self.height, self.width, 4).copy()
