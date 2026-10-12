import io
import struct
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
        self.last_dx_init_try = 0
        self.last_gpu_frame = None

        self.last_grid = None
        self.last_send_time = 0
        self.last_keyframe_time = 0
        self.last_delta_time = 0
        self.needs_settling_keyframe = False

        self._update_monitor()
        self._init_dxcam()

    def _init_dxcam(self):
        """Attempts to initialize GPU-direct DXGI Desktop Duplication capture."""
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
        except Exception:
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
        self.last_grid = None  # Force full keyframe on next capture

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

    def _grab_bgra_array(self, force=False, now=0.0):
        """Grabs a (H, W, 4) BGRA numpy array using DXGI GPU Duplication first, falling back to Win32 GDI (mss)."""
        if ensure_desktop_access(before_switch_callback=self._release_capture_resources):
            self.sct = mss.MSS()
            self._update_monitor()
            self.last_grid = None
            self.last_gpu_frame = None
            self.last_dx_init_try = 0
            self._init_dxcam()

        # 1. Try GPU-direct DXGI Desktop Duplication (dxcam)
        if HAS_DXCAM:
            if self.dx_cam is None and (now - self.last_dx_init_try >= 5.0):
                self._init_dxcam()
            if self.dx_cam is not None:
                try:
                    gpu_frame = self.dx_cam.grab()
                    if gpu_frame is not None:
                        self.last_gpu_frame = gpu_frame
                        return gpu_frame
                    # gpu_frame is None -> DXGI reports 0 pixels changed on GPU since last grab!
                    if self.last_gpu_frame is not None:
                        if force or (self.needs_settling_keyframe and (now - self.last_delta_time >= 0.35)) or (now - self.last_send_time >= 2.0):
                            return self.last_gpu_frame
                        return None
                except Exception:
                    try:
                        self.dx_cam.release()
                    except Exception:
                        pass
                    self.dx_cam = None

        # 2. Fallback to Win32 GDI BitBlt (mss) — works on UAC / Secure Desktop / initial frame
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
                return None

        if HAS_CV2:
            arr = np.frombuffer(raw.raw, dtype=np.uint8).reshape((raw.height, raw.width, 4))
            self.last_gpu_frame = arr
            return arr
        return raw

    def capture_frame(self, draw_cursor=False, force=False):
        """Captures a frame and returns either:
        - Raw JPEG bytes (0xFF 0xD8...) for a Full Keyframe
        - 'DL' + uint16(x, y, w, h) + JPEG bytes for a Delta Bounding-Box update
        - None if the screen has not changed."""
        now = time.time()
        grabbed = self._grab_bgra_array(force=force, now=now)
        if grabbed is None:
            return None

        if HAS_CV2 and isinstance(grabbed, np.ndarray):
            try:
                img_np = grabbed
                if self.scale != 1.0 and 0.1 <= self.scale < 1.0:
                    new_w = max(100, int(self.width * self.scale))
                    new_h = max(100, int(self.height * self.scale))
                    img_np = cv2.resize(img_np, (new_w, new_h), interpolation=cv2.INTER_AREA)

                fh, fw = img_np.shape[:2]
                if not img_np.flags["C_CONTIGUOUS"]:
                    img_np = np.ascontiguousarray(img_np)

                # Ultra-fast uint32 strided grid view (::4, ::4) -> ~0.2ms for 1080p
                grid = img_np.view(np.uint32).reshape(fh, fw)[::4, ::4]

                encode_params = [
                    int(cv2.IMWRITE_JPEG_QUALITY), self.quality,
                    int(cv2.IMWRITE_JPEG_OPTIMIZE), 0
                ]

                # Check if we must send a full keyframe
                force_keyframe = (
                    force
                    or self.last_grid is None
                    or self.last_grid.shape != grid.shape
                    or (now - self.last_keyframe_time >= 3.0 and self.needs_settling_keyframe)
                )

                if not force_keyframe:
                    ys, xs = np.nonzero(grid != self.last_grid)
                    if len(xs) == 0:
                        # No change detected on grid
                        if self.needs_settling_keyframe and (now - self.last_delta_time >= 0.35):
                            # Send one crisp settling full keyframe after activity stops
                            force_keyframe = True
                        elif now - self.last_send_time >= 2.0:
                            # Heartbeat keepalive frame
                            force_keyframe = True
                        else:
                            return None
                    else:
                        # Screen changed! Calculate dirty bounding box with 16px safety padding
                        x0 = max(0, int(xs.min()) * 4 - 16)
                        y0 = max(0, int(ys.min()) * 4 - 16)
                        x1 = min(fw, int(xs.max()) * 4 + 20)
                        y1 = min(fh, int(ys.max()) * 4 + 20)
                        bw = x1 - x0
                        bh = y1 - y0

                        # If dirty region is < 65% of screen area, send a compact Delta Patch!
                        if (bw * bh) < 0.65 * (fw * fh):
                            self.last_grid = grid.copy()
                            self.last_send_time = now
                            self.last_delta_time = now
                            self.needs_settling_keyframe = True

                            patch = img_np[y0:y1, x0:x1]
                            _, enc = cv2.imencode(".jpg", patch, encode_params)
                            header = b"DL" + struct.pack(">HHHH", x0, y0, bw, bh)
                            return header + enc.tobytes()
                        else:
                            force_keyframe = True

                # Encode and send Full Keyframe
                self.last_grid = grid.copy()
                self.last_send_time = now
                self.last_keyframe_time = now
                self.needs_settling_keyframe = False

                _, enc = cv2.imencode(".jpg", img_np, encode_params)
                return enc.tobytes()
            except Exception:
                return None

        # Fallback to PIL if OpenCV is not installed
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

