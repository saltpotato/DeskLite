import io
import logging
import time
import mss
import sys
import ctypes
import os
import importlib
from common import ensure_desktop_access, impersonate_interactive_user, revert_to_self

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

        self.width = 1920
        self.height = 1080
        self.left = 0
        self.top = 0
        self.monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}

        try:
            self.sct = mss.MSS()
        except Exception as e:
            logging.error(f"mss init error: {e}")
            self.sct = None
        self.dx_cam = None
        self.last_dx_init_try = 0.0

        self.last_grid = None
        self.last_send_time = 0.0

        # Hot-reload tracking for vision_framework.py
        self.vision_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vision_framework.py")
        self.reload_trigger_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".reload_vision")
        self.last_vision_mtime = os.path.getmtime(self.vision_path) if os.path.exists(self.vision_path) else 0.0
        self.last_reload_check = 0.0

        try:
            import vision_framework
            self.vision_pipeline = vision_framework.VisionHostPipeline()
        except Exception as e:
            logging.error(f"VisionHostPipeline init failed: {e}")
            self.vision_pipeline = None

        self._update_monitor()

    def reload_vision_pipeline(self) -> bool:
        """Dynamically reloads `vision_framework.py` in-memory without restarting the main process."""
        try:
            import vision_framework
            importlib.reload(vision_framework)
            self.vision_pipeline = vision_framework.VisionHostPipeline()
            self.last_grid = None
            if os.path.exists(self.vision_path):
                self.last_vision_mtime = os.path.getmtime(self.vision_path)
            logging.info("[HOT-RELOAD] 🚀 vision_framework.py successfully reloaded in-memory!")
            return True
        except Exception as e:
            logging.error(f"[HOT-RELOAD] ❌ Failed to reload vision_framework: {e}")
            return False

    def check_hot_reload(self, now: float = 0.0):
        """Checks every 0.8s if vision_framework.py was modified or if .reload_vision trigger exists."""
        if now == 0.0:
            now = time.time()
        if now - self.last_reload_check < 0.8:
            return
        self.last_reload_check = now

        # 1. Trigger file check (allows external scripts or non-admin shells to trigger reload instantly)
        if os.path.exists(self.reload_trigger_path):
            try:
                os.remove(self.reload_trigger_path)
            except Exception:
                pass
            self.reload_vision_pipeline()
            return

        # 2. File mtime auto-reload check
        if os.path.exists(self.vision_path):
            try:
                mtime = os.path.getmtime(self.vision_path)
                if self.last_vision_mtime and mtime > self.last_vision_mtime + 0.1:
                    logging.info(f"[HOT-RELOAD] Detected modification on {self.vision_path} (mtime updated)")
                    self.reload_vision_pipeline()
            except Exception:
                pass

    def reset_vision_state(self):
        if self.vision_pipeline is not None:
            self.vision_pipeline.reset()

    def notify_input(self, evt):
        if evt.get("event") in ("reload_vision", "hot_reload"):
            self.reload_vision_pipeline()
            return
        if self.vision_pipeline is not None:
            self.vision_pipeline.notify_input(evt)

    @staticmethod
    def _encode_jpeg(arr, quality):
        _, enc = cv2.imencode(".jpg", arr, [
            int(cv2.IMWRITE_JPEG_QUALITY), int(quality),
            int(cv2.IMWRITE_JPEG_OPTIMIZE), 0
        ])
        return enc.tobytes()

    def _init_dxcam(self):
        """Lazily initializes GPU-direct DXGI Desktop Duplication on the active capture thread.
        Impersonates the interactive user token during DuplicateOutput because Windows DXGI
        blocks raw NT AUTHORITY\\SYSTEM tokens with E_ACCESSDENIED."""
        if not HAS_DXCAM or sys.platform != "win32":
            return
        now = time.time()
        if now - self.last_dx_init_try < 5.0:
            return
        self.last_dx_init_try = now
        impersonated = False
        try:
            if self.dx_cam is not None:
                try:
                    self.dx_cam.release()
                except Exception:
                    pass
                self.dx_cam = None
            impersonated = impersonate_interactive_user()
            out_idx = max(0, self.monitor_index - 1)
            self.dx_cam = dxcam.create(output_idx=out_idx, output_color="BGRA")
            logging.info("DXGI Desktop Duplication (dxcam) GPU capture active.")
        except Exception as e:
            logging.debug(f"DXGI init fallback to GDI: {e}")
            self.dx_cam = None
        finally:
            if impersonated:
                revert_to_self()

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
        self.reset_vision_state()

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
                    if not force and (now - self.last_send_time < 1.5):
                        return None, False
                except Exception:
                    try:
                        self.dx_cam.release()
                    except Exception:
                        pass
                    self.dx_cam = None

        # 2. Fallback / UAC Secure Desktop path: Win32 GDI BitBlt (mss)
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
        """Captures a live frame via DXGI GPU Duplication (with GDI fallback) and encodes via VisionHostPipeline."""
        now = time.time()
        self.check_hot_reload(now)
        grabbed, from_dxgi_update = self._grab_frame_array(force=force, now=now)
        if grabbed is None:
            return None

        if HAS_CV2 and isinstance(grabbed, np.ndarray):
            try:
                img_np = grabbed

                # Only run the CPU uint32 grid diff check when using the GDI fallback path!
                # When from_dxgi_update is True, the DXGI Desktop Duplication API already guaranteed a GPU change.
                if not from_dxgi_update:
                    fh, fw = img_np.shape[:2]
                    grid = img_np.view(np.uint32).reshape(fh, fw)[::4, ::4]
                    if not force and self.last_grid is not None and self.last_grid.shape == grid.shape:
                        if not np.any(grid != self.last_grid) and (now - self.last_send_time < 1.5):
                            return None
                    self.last_grid = grid.copy()

                self.last_send_time = now

                if self.scale != 1.0 and 0.1 <= self.scale < 1.0:
                    new_w = max(100, int(self.width * self.scale))
                    new_h = max(100, int(self.height * self.scale))
                    img_np = cv2.resize(img_np, (new_w, new_h), interpolation=cv2.INTER_AREA)

                if self.vision_pipeline is not None:
                    return self.vision_pipeline.encode_frame(img_np, force, self._encode_jpeg, self.quality)

                return self._encode_jpeg(img_np, self.quality)
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



