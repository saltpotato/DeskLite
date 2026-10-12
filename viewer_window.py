import asyncio
import collections
import io
import json
import logging
import struct
import threading
import time
import tkinter as tk
from tkinter import messagebox
import websockets
from PIL import Image, ImageTk
from common import get_clipboard_text, set_clipboard_text

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
    HAS_WEBRTC = True
except ImportError:
    HAS_WEBRTC = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [VIEWER] %(message)s")


class RemoteDesktopViewer(tk.Toplevel):
    def __init__(self, master, relay_url, target_id, password=""):
        super().__init__(master)
        self.relay_url = relay_url
        self.target_id = target_id
        self.password = password

        self.title(f"Remote Session: {target_id}")
        self.geometry("1280x720")
        self.minsize(640, 480)

        self.remote_width = 1920
        self.remote_height = 1080

        self.canvas_w = 1280
        self.canvas_h = 720
        self.img_pos_x = 0
        self.img_pos_y = 0
        self.img_draw_w = 1280
        self.img_draw_h = 720

        self.current_photo = None
        self.canvas_img_id = None
        self.latest_frame = None
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
        self.last_mouse_time = 0
        self.last_clipboard = ""
        self.frame_queue = collections.deque(maxlen=30)

        self._build_ui()
        self._bind_events()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Start connection in background thread
        self.worker_thread = threading.Thread(target=self._run_async_worker, daemon=True)
        self.worker_thread.start()

    def _build_ui(self):
        # Top toolbar
        self.toolbar = tk.Frame(self, bg="#2c3e50", height=32)
        self.toolbar.pack(fill=tk.X, side=tk.TOP)

        self.status_label = tk.Label(self.toolbar, text="Connecting...", bg="#2c3e50", fg="white", font=("Segoe UI", 9))
        self.status_label.pack(side=tk.LEFT, padx=10, pady=4)

        self.btn_fullscreen = tk.Button(self.toolbar, text="⛶ Fullscreen", bg="#34495e", fg="white",
                                        relief=tk.FLAT, font=("Segoe UI", 8), command=self.toggle_fullscreen)
        self.btn_fullscreen.pack(side=tk.RIGHT, padx=4, pady=4)

        self.btn_desktop = tk.Button(self.toolbar, text="🖥️ Desktop", bg="#34495e", fg="white",
                                     relief=tk.FLAT, font=("Segoe UI", 8), command=lambda: self.send_shortcut("desktop"))
        self.btn_desktop.pack(side=tk.RIGHT, padx=3, pady=4)

        self.btn_clip = tk.Button(self.toolbar, text="📋 Paste", bg="#34495e", fg="white",
                                  relief=tk.FLAT, font=("Segoe UI", 8), command=self.send_clipboard_to_host)
        self.btn_clip.pack(side=tk.RIGHT, padx=3, pady=4)
        self.btn_explorer = tk.Button(self.toolbar, text="📁 Explorer", bg="#34495e", fg="white",
                                      relief=tk.FLAT, font=("Segoe UI", 8), command=lambda: self.send_shortcut("explorer"))
        self.btn_explorer.pack(side=tk.RIGHT, padx=3, pady=4)

        self.btn_win = tk.Button(self.toolbar, text="🪟 Start", bg="#34495e", fg="white",
                                 relief=tk.FLAT, font=("Segoe UI", 8), command=lambda: self.send_shortcut("win_key"))
        self.btn_win.pack(side=tk.RIGHT, padx=3, pady=4)

        self.btn_ctrl_alt_del = tk.Button(self.toolbar, text="Ctrl+Alt+Del", bg="#34495e", fg="white",
                                          relief=tk.FLAT, font=("Segoe UI", 8), command=self.send_ctrl_alt_del)
        self.btn_ctrl_alt_del.pack(side=tk.RIGHT, padx=3, pady=4)

        self.btn_hq = tk.Button(self.toolbar, text="💎 HQ", bg="#8e44ad", fg="white",
                                relief=tk.FLAT, font=("Segoe UI", 8), command=lambda: self.set_quality(85, 1.0))
        self.btn_hq.pack(side=tk.RIGHT, padx=3, pady=4)

        self.btn_balanced = tk.Button(self.toolbar, text="⚖️ Balanced", bg="#2980b9", fg="white",
                                      relief=tk.FLAT, font=("Segoe UI", 8), command=lambda: self.set_quality(75, 1.0))
        self.btn_balanced.pack(side=tk.RIGHT, padx=3, pady=4)

        self.btn_fast = tk.Button(self.toolbar, text="🚀 Fast", bg="#27ae60", fg="white",
                                  relief=tk.FLAT, font=("Segoe UI", 8), command=lambda: self.set_quality(65, 1.0))
        self.btn_fast.pack(side=tk.RIGHT, padx=3, pady=4)

        # Canvas with hardware cursor enabled (0ms lag!)
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0, cursor="arrow")
        self.canvas.pack(fill=tk.BOTH, expand=True)

    def _bind_events(self):
        self.canvas.bind("<Configure>", self.on_canvas_resize)

        # Mouse events (ultra-responsive 80Hz updates)
        self.canvas.bind("<Motion>", self.on_mouse_move)
        self.canvas.bind("<B1-Motion>", self.on_mouse_move)
        self.canvas.bind("<B2-Motion>", self.on_mouse_move)
        self.canvas.bind("<B3-Motion>", self.on_mouse_move)

        self.canvas.bind("<ButtonPress-1>", lambda e: self.on_mouse_button(e, "down", "left"))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self.on_mouse_button(e, "up", "left"))
        self.canvas.bind("<ButtonPress-2>", lambda e: self.on_mouse_button(e, "down", "middle"))
        self.canvas.bind("<ButtonRelease-2>", lambda e: self.on_mouse_button(e, "up", "middle"))
        self.canvas.bind("<ButtonPress-3>", lambda e: self.on_mouse_button(e, "down", "right"))
        self.canvas.bind("<ButtonRelease-3>", lambda e: self.on_mouse_button(e, "up", "right"))

        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)  # Windows
        self.canvas.bind("<Button-4>", lambda e: self.on_mouse_wheel_linux(e, 1))  # Linux up
        self.canvas.bind("<Button-5>", lambda e: self.on_mouse_wheel_linux(e, -1))  # Linux down

        # Keyboard events
        self.bind("<KeyPress>", self.on_key_press)
        self.bind("<KeyRelease>", self.on_key_release)
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<FocusIn>", lambda e: self._check_clipboard_sync())

    def toggle_fullscreen(self):
        is_fs = self.attributes("-fullscreen")
        self.attributes("-fullscreen", not is_fs)

    def send_shortcut(self, name):
        self.send_input({"event": "shortcut", "name": name})

    def send_clipboard_to_host(self):
        text = get_clipboard_text()
        if text and self.loop and self.loop.is_running():
            self.last_clipboard = text
            msg = json.dumps({"type": "clipboard", "text": text})
            asyncio.run_coroutine_threadsafe(self._ws_send(msg), self.loop)
            self.status_label.config(text="📋 Clipboard sent to host")

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
            self.status_label.config(text=f"Profile active: Quality {quality}%, Scale {int(scale*100)}%")

    def on_canvas_resize(self, event):
        self.canvas_w = event.width
        self.canvas_h = event.height

    def _calc_normalized_coords(self, event_x, event_y):
        if self.img_draw_w <= 0 or self.img_draw_h <= 0:
            return None, None
        rel_x = event_x - self.img_pos_x
        rel_y = event_y - self.img_pos_y

        if 0 <= rel_x <= self.img_draw_w and 0 <= rel_y <= self.img_draw_h:
            norm_x = rel_x / float(self.img_draw_w)
            norm_y = rel_y / float(self.img_draw_h)
            return min(1.0, max(0.0, norm_x)), min(1.0, max(0.0, norm_y))
        return None, None

    def on_mouse_move(self, event):
        now = time.time()
        # Fast 80Hz throttle (~12ms) for smooth tracking
        if now - self.last_mouse_time < 0.012:
            return
        self.last_mouse_time = now

        nx, ny = self._calc_normalized_coords(event.x, event.y)
        if nx is not None:
            self.send_input({"event": "mouse_move", "x": nx, "y": ny})

    def on_mouse_button(self, event, action, button):
        self.canvas.focus_set()
        nx, ny = self._calc_normalized_coords(event.x, event.y)
        if nx is not None:
            self.send_input({"event": "mouse_move", "x": nx, "y": ny})
            self.send_input({"event": f"mouse_{action}", "button": button})

    def on_mouse_wheel(self, event):
        delta = 1 if event.delta > 0 else -1
        self.send_input({"event": "mouse_wheel", "delta": delta})

    def on_mouse_wheel_linux(self, event, delta):
        self.send_input({"event": "mouse_wheel", "delta": delta})

    def on_key_press(self, event):
        self.send_input({"event": "key_down", "key": event.keysym, "char": event.char})

    def on_key_release(self, event):
        self.send_input({"event": "key_up", "key": event.keysym, "char": event.char})

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
                self.after(0, lambda: self.status_label.config(text="🟡 Connection lost – reconnecting in 2s..."))
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
            self.after_idle(self._process_latest_frame)

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
                self.after(0, lambda: self.status_label.config(text="🟡 Host disconnected – waiting for reconnect..."))
                return False
            elif msg_type == "clipboard":
                text = meta.get("text", "")
                if text and text != self.last_clipboard:
                    self.last_clipboard = text
                    set_clipboard_text(text)
                    self.after(0, lambda: self.status_label.config(text="📋 Clipboard synced from host"))
        except Exception:
            pass
        return True

    async def _upgrade_to_p2p(self, endpoints, token, webrtc_ok):
        """Upgrades the session from VPS signaling to a Direct P2P connection (LAN/VPN WebSocket or WebRTC STUN)."""
        # 1. Try fast Direct WebSocket P2P endpoints first (LAN / VPN / localhost)
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
                        self.after(0, lambda: self.status_label.config(
                            text=f"🟢 Direct P2P Connected to {self.target_id} (0 KB/s on VPS)"
                        ))
                        asyncio.create_task(self._read_direct_p2p_ws(p2p_sock))
                        return
                    else:
                        await p2p_sock.close()
                except Exception:
                    continue

        # 2. Try WebRTC STUN UDP Hole Punching across NAT routers over the internet
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
                    self.after(0, lambda: self.status_label.config(
                        text=f"🟢 Direct P2P WebRTC Connected to {self.target_id} (0 KB/s on VPS)"
                    ))

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
                        self.after(0, lambda: self._show_error(err_msg))
                        return
                    return

                self.after(0, lambda: self.status_label.config(text=f"Connected to {self.target_id} (Negotiating Direct P2P...)"))

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
                    if getattr(self, "base_image", None) is not None:
                        dx, dy, dw, dh = struct.unpack(">HHHH", frame_bytes[2:10])
                        patch = Image.open(io.BytesIO(frame_bytes[10:]))
                        self.base_image.paste(patch, (dx, dy))
                else:
                    img = Image.open(io.BytesIO(frame_bytes))
                    img.load()
                    self.base_image = img

            pil_img = getattr(self, "base_image", None)
            if pil_img is None:
                return

            cw = max(100, self.canvas_w)
            ch = max(100, self.canvas_h)

            aspect = pil_img.width / float(pil_img.height)
            if cw / float(ch) > aspect:
                draw_h = ch
                draw_w = int(ch * aspect)
            else:
                draw_w = cw
                draw_h = int(cw / aspect)

            self.img_draw_w = draw_w
            self.img_draw_h = draw_h
            self.img_pos_x = (cw - draw_w) // 2
            self.img_pos_y = (ch - draw_h) // 2

            resized = pil_img.resize((draw_w, draw_h), Image.Resampling.BILINEAR)

            photo = ImageTk.PhotoImage(resized)
            self.current_photo = photo

            if not self.canvas_img_id:
                self.canvas_img_id = self.canvas.create_image(self.img_pos_x, self.img_pos_y, anchor=tk.NW, image=photo)
            else:
                self.canvas.coords(self.canvas_img_id, self.img_pos_x, self.img_pos_y)
                self.canvas.itemconfig(self.canvas_img_id, image=photo)

            self.fps_count += 1
            now = time.time()
            if now - self.fps_timer >= 1.0:
                self.fps_display = self.fps_count
                self.fps_count = 0
                self.fps_timer = now
                fps_text = f"{self.transport_mode} | {self.target_id} | {self.remote_width}x{self.remote_height} | {self.fps_display} FPS"
                self.status_label.config(text=fps_text)

        except Exception as e:
            logging.debug(f"Frame decode error: {e}")

    def _show_error(self, message):
        messagebox.showerror("Connection Error", message, parent=self)
        self.destroy()

    def on_close(self):
        self.running = False
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.destroy()
