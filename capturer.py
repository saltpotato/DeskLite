import io
import logging
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

try:
    import dxcam
    HAS_DXCAM = True
except ImportError:
    HAS_DXCAM = False

from PIL import Image

class ScreenCapturer:
    def __init__(self, monitor_index=1, quality=75, scale=1.0):
        ensure_desktop_access()
        self.quality = quality
        self.scale = scale
        self.monitor_index = monitor_index

        self.sct = mss.MSS()
        self.dx_cam = None
        self.last_dx_init_try = 0.0

        self.last_grid = None
        self.last_send_time = 0.0

        self._update_monitor()

    def _init_dxcam(self):
        """Lazily initializes GPU-direct DXGI Desktop Duplication on the active capture thread."""
        if not HAS_DXCAM or sys.platform != "win32":
            return
        now = time.time()
        if now - self.last_dx_init_try < 5.0:
            return
        self.last_dx_init_try = now
        try:
            if self.dx_cam is not None:
                try:
                    self.dx_cam.release()
                except Exception:
                    pass
                self.dx_cam = None
            out_idx = max(0, self.monitor_index - 1)
            self.dx_cam = dxcam.create(output_idx=out_idx, output_color="BGRA")
            logging.info("DXGI Desktop Duplication (dxcam) GPU capture active.")
        except Exception as e:
            logging.debug(f"DXGI init fallback to GDI: {e}")
            self.dx_cam = None

    def _release_capture_resources(self):
        try:
            self.sct.close()
        except Exception:
            pass
        if self.dx_cam is not None:
            try:
                self.dx_cam.release()
            except Exception:
                pass
            self.dx_cam = None

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

    def _grab_frame_array(self, force=False, now=0.0):
        """Grabs a live (H, W, 4) BGRA frame using DXGI Desktop Duplication API (dxcam)
        with automatic GDI (mss) fallback for UAC prompts and forced post-click updates."""
        if ensure_desktop_access(before_switch_callback=self._release_capture_resources):
            self.sct = mss.MSS()
            self._update_monitor()
            self.last_grid = None
            self.last_dx_init_try = 0.0

        # 1. Primary path: GPU-direct DXGI Desktop Duplication API (dxcam)
        if HAS_DXCAM and HAS_CV2:
            if self.dx_cam is None and (now - self.last_dx_init_try >= 5.0):
                self._init_dxcam()
            if self.dx_cam is not None:
                try:
                    # Zero-copy GPU grab: returns new frame if GPU surface updated, else None in ~0.05ms
                    gpu_frame = self.dx_cam.grab(copy=False, new_frame_only=True)
                    if gpu_frame is not None:
                        return gpu_frame, True
                    # If no new GPU frame is flagged and we don't need a forced/heartbeat frame, skip immediately!
                    if not force and self.last_grid is not None and (now - self.last_send_time < 1.5):
                        return None, False
                except Exception:
                    try:
                        self.dx_cam.release()
                    except Exception:
                        pass
                    self.dx_cam = None

        # 2. Fallback / Force-verification path: Win32 GDI BitBlt (mss)
        try:
            raw = self.sct.grab(self.monitor)
        except Exception:
            try:
                ensure_desktop_access()
                self.sct = mss.MSS()
                self._update_monitor()
                self.last_grid = None
                raw = self.sct.grab(self.monitor)
            except Exception:
                return None, False

        if HAS_CV2:
            arr = np.frombuffer(raw.raw, dtype=np.uint8).reshape((raw.height, raw.width, 4))
            return arr, False
        return raw, False

    def capture_frame(self, draw_cursor=False, force=False):
        """Captures a live frame via DXGI GPU Duplication (with GDI fallback) and encodes to JPEG."""
        now = time.time()
        grabbed, from_dxgi_update = self._grab_frame_array(force=force, now=now)
        if grabbed is None:
            return None

        if HAS_CV2 and isinstance(grabbed, np.ndarray):
            try:
                img_np = grabbed
                fh, fw = img_np.shape[:2]

                # Ultra-fast 0.16ms uint32 strided grid view (::4, ::4)
                grid = img_np.view(np.uint32).reshape(fh, fw)[::4, ::4]

                if not force and not from_dxgi_update and self.last_grid is not None and self.last_grid.shape == grid.shape:
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
            img = Image.frombytes("RGB", grabbed.size, grabbed.bgra, "raw", "BGRX")

            if self.scale != 1.0 and 0.1 <= self.scale < 1.0:
                new_w = max(100, int(self.width * self.scale))
                new_h = max(100, int(self.height * self.scale))
                img = img.resize((new_w, new_h), Image.Resampling.BILINEAR)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self.quality, optimize=False)
            return buf.getvalue()
        except Exception:
            return None



