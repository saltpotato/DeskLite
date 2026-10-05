import asyncio
import io
import json
import logging
import threading
import time
import tkinter as tk
from tkinter import messagebox
import websockets
from PIL import Image, ImageTk

logging.basicConfig(level=logging.INFO, format="%(asctime)s [VIEWER] %(message)s")

class RemoteDesktopViewer(tk.Toplevel):
    def __init__(self, master, relay_url, target_id, password=""):
        super().__init__(master)
        self.relay_url = relay_url
        self.target_id = target_id
        self.password = password
        
        self.title(f"Fernsteuerung: {target_id}")
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
        self.loop = None
        
        self.fps_count = 0
        self.fps_timer = time.time()
        self.fps_display = 0
        self.last_mouse_time = 0

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
        
        self.status_label = tk.Label(self.toolbar, text="Verbinde...", bg="#2c3e50", fg="white", font=("Segoe UI", 9))
        self.status_label.pack(side=tk.LEFT, padx=10, pady=4)
        
        self.btn_fullscreen = tk.Button(self.toolbar, text="⛶ Vollbild", bg="#34495e", fg="white",
                                        relief=tk.FLAT, font=("Segoe UI", 8), command=self.toggle_fullscreen)
        self.btn_fullscreen.pack(side=tk.RIGHT, padx=6, pady=4)

        self.btn_ctrl_alt_del = tk.Button(self.toolbar, text="Ctrl+Alt+Entf", bg="#34495e", fg="white",
                                          relief=tk.FLAT, font=("Segoe UI", 8), command=self.send_ctrl_alt_del)
        self.btn_ctrl_alt_del.pack(side=tk.RIGHT, padx=4, pady=4)

        self.btn_hq = tk.Button(self.toolbar, text="💎 HQ", bg="#8e44ad", fg="white",
                                relief=tk.FLAT, font=("Segoe UI", 8), command=lambda: self.set_quality(75, 1.0))
        self.btn_hq.pack(side=tk.RIGHT, padx=3, pady=4)

        self.btn_balanced = tk.Button(self.toolbar, text="⚖️ Normal", bg="#2980b9", fg="white",
                                      relief=tk.FLAT, font=("Segoe UI", 8), command=lambda: self.set_quality(60, 0.75))
        self.btn_balanced.pack(side=tk.RIGHT, padx=3, pady=4)

        self.btn_turbo = tk.Button(self.toolbar, text="🚀 Turbo", bg="#27ae60", fg="white",
                                   relief=tk.FLAT, font=("Segoe UI", 8), command=lambda: self.set_quality(45, 0.60))
        self.btn_turbo.pack(side=tk.RIGHT, padx=3, pady=4)

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
        
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel) # Windows
        self.canvas.bind("<Button-4>", lambda e: self.on_mouse_wheel_linux(e, 1)) # Linux up
        self.canvas.bind("<Button-5>", lambda e: self.on_mouse_wheel_linux(e, -1)) # Linux down
        
        # Keyboard events
        self.bind("<KeyPress>", self.on_key_press)
        self.bind("<KeyRelease>", self.on_key_release)
        self.bind("<F11>", lambda e: self.toggle_fullscreen())

    def toggle_fullscreen(self):
        is_fs = self.attributes("-fullscreen")
        self.attributes("-fullscreen", not is_fs)

    def send_ctrl_alt_del(self):
        self.send_input({"event": "key_down", "key": "ctrl"})
        self.send_input({"event": "key_down", "key": "alt"})
        self.send_input({"event": "key_down", "key": "delete"})
        self.send_input({"event": "key_up", "key": "delete"})
        self.send_input({"event": "key_up", "key": "alt"})
        self.send_input({"event": "key_up", "key": "ctrl"})

    def set_quality(self, quality, scale):
        if self.ws and self.loop and self.loop.is_running():
            msg = json.dumps({"type": "set_quality", "quality": quality, "scale": scale})
            asyncio.run_coroutine_threadsafe(self._ws_send(msg), self.loop)
            self.status_label.config(text=f"Modus aktiv: Qualität {quality}%, Skalierung {int(scale*100)}%")

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
        if self.ws and self.loop and self.loop.is_running():
            msg = json.dumps({"type": "input", "data": data})
            asyncio.run_coroutine_threadsafe(self._ws_send(msg), self.loop)

    async def _ws_send(self, msg):
        try:
            if self.ws:
                await self.ws.send(msg)
        except Exception:
            pass

    def _run_async_worker(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect_and_view())

    async def _connect_and_view(self):
        try:
            async with websockets.connect(
                self.relay_url,
                max_size=20 * 1024 * 1024,
                ping_interval=15,
                ping_timeout=15
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
                    err_msg = resp.get("message", "Verbindung abgelehnt")
                    self.after(0, lambda: self._show_error(err_msg))
                    return

                self.after(0, lambda: self.status_label.config(text=f"Verbunden mit {self.target_id}"))

                async for message in ws:
                    if not self.running:
                        break
                    
                    if isinstance(message, bytes):
                        # Store latest frame and trigger render on idle (Anti-Lag / Drop old frames)
                        self.latest_frame = message
                        if not self.render_pending:
                            self.render_pending = True
                            self.after_idle(self._process_latest_frame)

                    elif isinstance(message, str):
                        try:
                            meta = json.loads(message)
                            if meta.get("type") == "screen_info":
                                self.remote_width = meta.get("width", 1920)
                                self.remote_height = meta.get("height", 1080)
                            elif meta.get("type") == "host_disconnected":
                                self.after(0, lambda: self._show_error("Host hat die Verbindung beendet."))
                                break
                        except Exception:
                            pass
        except Exception as e:
            if self.running:
                self.after(0, lambda: self._show_error(f"Verbindungsfehler: {e}"))

    def _process_latest_frame(self):
        self.render_pending = False
        if not self.running or not self.latest_frame:
            return
        
        frame_bytes = self.latest_frame
        try:
            pil_img = Image.open(io.BytesIO(frame_bytes))
            
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

            # Fast bilinear resize
            resized = pil_img.resize((draw_w, draw_h), Image.Resampling.BILINEAR)
            
            photo = ImageTk.PhotoImage(resized)
            self.current_photo = photo
            
            # Efficient item update instead of delete("all")
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
                fps_text = f"Verbunden mit {self.target_id} | {self.remote_width}x{self.remote_height} | {self.fps_display} FPS (Niedrige Latenz)"
                self.status_label.config(text=fps_text)

        except Exception as e:
            logging.debug(f"Frame-Decodierfehler: {e}")

    def _show_error(self, message):
        messagebox.showerror("Verbindungsfehler", message, parent=self)
        self.destroy()

    def on_close(self):
        self.running = False
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.destroy()
