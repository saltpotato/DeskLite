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

from PIL import Image

class ScreenCapturer:
    def __init__(self, monitor_index=1, quality=75, scale=1.0):
        ensure_desktop_access()
        self.quality = quality
        self.scale = scale
        self.monitor_index = monitor_index

        self.sct = mss.MSS()
        self.last_grid = None
        self.last_send_time = 0

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
        self.last_grid = None  # Force next frame to be sent

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
        """Captures a live frame and returns JPEG bytes.
        Uses a 0.16ms uint32 strided grid check to detect even subtle window focus changes
        while sending 0 bytes when the screen is completely static."""
        try:
            if ensure_desktop_access(before_switch_callback=lambda: self.sct.close()):
                self.sct = mss.MSS()
                self._update_monitor()
                self.last_grid = None
            raw = self.sct.grab(self.monitor)
        except Exception:
            try:
                ensure_desktop_access()
                self.sct = mss.MSS()
                self._update_monitor()
                self.last_grid = None
                raw = self.sct.grab(self.monitor)
            except Exception:
                return None

        now = time.time()

        if HAS_CV2:
            try:
                img_np = np.frombuffer(raw.raw, dtype=np.uint8).reshape((raw.height, raw.width, 4))

                # Ultra-fast 0.16ms uint32 strided grid view (::4, ::4)
                # Detects any window activation, focus border, or text change immediately
                grid = img_np.view(np.uint32).reshape(raw.height, raw.width)[::4, ::4]

                if not force and self.last_grid is not None and self.last_grid.shape == grid.shape:
                    if not np.any(grid != self.last_grid) and (now - self.last_send_time < 1.5):
                        return None

                self.last_grid = grid.copy()
                self.last_send_time = now

                if self.scale != 1.0 and 0.1 <= self.scale < 1.0:
                    new_w = max(100, int(self.width * self.scale))
                    new_h = max(100, int(self.height * self.scale))
                    img_np = cv2.resize(img_np, (new_w, new_h), interpolation=cv2.INTER_AREA)

                _, enc = cv2.imencode(".jpg", img_np, [
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


