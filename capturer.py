import io
import time
import mss
import sys
import ctypes
from common import ensure_desktop_access

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

from PIL import Image, ImageDraw

class ScreenCapturer:
    def __init__(self, monitor_index=1, quality=55, scale=0.75):
        ensure_desktop_access()
        self.quality = quality
        self.scale = scale
        self.sct = mss.MSS()
        
        self.last_thumb = None
        self.last_send_time = 0
        
        # Primary or selected monitor
        if monitor_index < len(self.sct.monitors):
            self.monitor = self.sct.monitors[monitor_index]
        else:
            self.monitor = self.sct.monitors[1]
            
        self.width = self.monitor["width"]
        self.height = self.monitor["height"]
        self.left = self.monitor["left"]
        self.top = self.monitor["top"]

    def update_settings(self, quality=None, scale=None):
        if quality is not None:
            self.quality = max(20, min(95, int(quality)))
        if scale is not None:
            self.scale = max(0.2, min(1.0, float(scale)))
        self.last_thumb = None  # Force next frame to be sent

    def get_cursor_pos(self):
        """Returns cursor coordinates relative to monitor."""
        if sys.platform == "win32":
            try:
                class POINT(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
                pt = POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                return pt.x - self.left, pt.y - self.top
            except Exception:
                return None
        return None

    def capture_frame(self, draw_cursor=False, force=False):
        """Captures a frame and returns JPEG bytes. Returns None if screen didn't change (smart skip)."""
        ensure_desktop_access()
        raw = self.sct.grab(self.monitor)
        now = time.time()

        if HAS_CV2 and not draw_cursor:
            img_np = np.frombuffer(raw.raw, dtype=np.uint8).reshape((raw.height, raw.width, 4))
            
            # Smart Diff Check: avoid wasteful CPU/bandwidth when screen is idle
            if not force:
                thumb = cv2.resize(img_np, (48, 27), interpolation=cv2.INTER_NEAREST)
                if self.last_thumb is not None:
                    diff = np.mean(np.abs(thumb.astype(np.int16) - self.last_thumb.astype(np.int16)))
                    # If unchanged and sent recently (< 1.5s), skip frame!
                    if diff < 0.2 and (now - self.last_send_time < 1.5):
                        return None
                self.last_thumb = thumb

            self.last_send_time = now

            if self.scale != 1.0 and 0.1 <= self.scale < 1.0:
                new_w = max(100, int(self.width * self.scale))
                new_h = max(100, int(self.height * self.scale))
                img_np = cv2.resize(img_np, (new_w, new_h), interpolation=cv2.INTER_AREA)

            _, enc = cv2.imencode('.jpg', img_np, [
                int(cv2.IMWRITE_JPEG_QUALITY), self.quality,
                int(cv2.IMWRITE_JPEG_OPTIMIZE), 0
            ])
            return enc.tobytes()

        # Fallback to PIL
        self.last_send_time = now
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        if draw_cursor:
            cur_pos = self.get_cursor_pos()
            if cur_pos and 0 <= cur_pos[0] < self.width and 0 <= cur_pos[1] < self.height:
                draw = ImageDraw.Draw(img)
                cx, cy = cur_pos
                points = [(cx, cy), (cx, cy + 18), (cx + 5, cy + 14), (cx + 12, cy + 12)]
                draw.polygon(points, fill="white", outline="black")

        if self.scale != 1.0 and 0.1 <= self.scale < 1.0:
            new_w = max(100, int(self.width * self.scale))
            new_h = max(100, int(self.height * self.scale))
            img = img.resize((new_w, new_h), Image.Resampling.BILINEAR)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self.quality, optimize=False)
        return buf.getvalue()
