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
        self.monitor_index = monitor_index
        
        self._update_monitor()

    def _update_monitor(self):
        try:
            if self.monitor_index < len(self.sct.monitors):
                self.monitor = self.sct.monitors[self.monitor_index]
            else:
                self.monitor = self.sct.monitors[1]
            self.width = self.monitor["width"]
            self.height = self.monitor["height"]
            self.left = self.monitor["left"]
            self.top = self.monitor["top"]
        except Exception:
            pass

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
        """Captures a frame and returns JPEG bytes. Resilient to screen standby and locks."""
        try:
            ensure_desktop_access()
            raw = self.sct.grab(self.monitor)
        except Exception:
            try:
                self.sct = mss.MSS()
                ensure_desktop_access()
                self._update_monitor()
                raw = self.sct.grab(self.monitor)
            except Exception:
                return None

        now = time.time()

        if HAS_CV2:
            try:
                img_np = np.frombuffer(raw.raw, dtype=np.uint8).reshape((raw.height, raw.width, 4))

                # Smart Diff Check: keep stream alive every 1.0s even when unchanged
                if not force:
                    thumb = cv2.resize(img_np, (48, 27), interpolation=cv2.INTER_NEAREST)
                    if self.last_thumb is not None:
                        diff = np.mean(np.abs(thumb.astype(np.int16) - self.last_thumb.astype(np.int16)))
                        if diff < 0.2 and (now - self.last_send_time < 1.0):
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
            except Exception:
                return None

        # Fallback to PIL
        try:
            self.last_send_time = now
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

            if self.scale != 1.0 and 0.1 <= self.scale < 1.0:
                new_w = max(100, int(self.width * self.scale))
                new_h = max(100, int(self.height * self.scale))
                img = img.resize((new_w, new_h), Image.Resampling.BILINEAR)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self.quality, optimize=False)
            return buf.getvalue()
        except Exception:
            return None
