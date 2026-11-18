"""Regression tests for long-running animation cycle detection and ShadowViewer compatibility."""

import math
import time
import unittest
import numpy as np
import cv2

from capturer import ScreenCapturer
from vision_framework import VisionHostPipeline


def _draw_spinner_frame(angle_deg: float, tail_extra: int = 0) -> np.ndarray:
    """Render a deterministic 72x72-class rotating dot spinner onto a 240x240 BGR canvas."""
    img = np.full((240, 240, 3), 24, dtype=np.uint8)
    cx, cy = 120.0, 120.0
    for dot in range(12):
        ang = math.radians(angle_deg + dot * 30.0)
        r = 22.0 + (tail_extra if dot == 11 else 0)
        dx = int(round(cx + r * math.cos(ang)))
        dy = int(round(cy + r * math.sin(ang)))
        intensity = int(70 + (dot / 11.0) * 185)
        cv2.circle(img, (dx, dy), 4, (intensity, intensity // 2 + 40, 40), -1)
    return img


class AnimationCycleTests(unittest.TestCase):
    def test_long_running_spinner_survives_beyond_120_frames(self):
        """Animations must keep hitting RAM cache well past 120 frames under shadow repair."""
        cap = ScreenCapturer.__new__(ScreenCapturer)
        cap.vision_enabled = True
        cap.shadow_repair_enabled = True
        cap.last_grid = None
        cap.last_send_time = 0.0
        cap.quality = 75
        cap.scale = 1.0
        from shadow_viewer import ShadowViewer
        cap.shadow_viewer = ShadowViewer()
        cap.vision_pipeline = VisionHostPipeline()
        cap.vision_pipeline.shared["debug_mode"] = False
        cap.vision_pipeline.shared["shadow_repair_active"] = True

        late_zero_kb_hits = 0
        late_png_repairs = 0

        for idx in range(260):
            frame = _draw_spinner_frame((idx % 12) * 30.0)
            pkt = cap.vision_pipeline.encode_frame(frame, False, cap._encode_jpeg, 75)
            out = cap.shadow_viewer.process(frame, pkt)
            if idx >= 120 and out is not None and out.startswith(b"VF"):
                if b'"anim_cycle"' in out or b'"bg_restore"' in out:
                    late_zero_kb_hits += 1
                if b"\x89PNG" in out:
                    late_png_repairs += 1

        self.assertGreaterEqual(late_zero_kb_hits, 130)
        self.assertEqual(late_png_repairs, 0)

    def test_45_phase_animation_fits_in_keyframe_budget(self):
        """Animations with >32 phases (e.g. 1.5s @ 30fps = 45 phases) must cache all phases."""
        pipeline = VisionHostPipeline()
        pipeline.shared["debug_mode"] = False
        late_hits = 0

        for idx in range(150):
            frame = _draw_spinner_frame((idx % 45) * 8.0)
            pkt = pipeline.encode_frame(frame, False, ScreenCapturer._encode_jpeg, 75)
            if idx >= 60 and pkt and pkt.startswith(b"VF") and (b'"anim_cycle"' in pkt or b'"bg_restore"' in pkt):
                late_hits += 1

        self.assertGreaterEqual(late_hits, 80)

    def test_expanding_bounding_box_does_not_clog_keyframes(self):
        """Growing spinner bounds must prune obsolete smaller keyframes instead of failing cycles."""
        pipeline = VisionHostPipeline()
        pipeline.shared["debug_mode"] = False
        late_hits = 0

        for idx in range(120):
            extra = 4 if (idx % 12 == 10 and idx >= 10) else 0
            frame = _draw_spinner_frame((idx % 12) * 30.0, tail_extra=extra)
            pkt = pipeline.encode_frame(frame, False, ScreenCapturer._encode_jpeg, 75)
            if idx >= 36 and pkt and pkt.startswith(b"VF") and (b'"anim_cycle"' in pkt or b'"bg_restore"' in pkt):
                late_hits += 1

        self.assertGreaterEqual(late_hits, 75)


if __name__ == "__main__":
    unittest.main()
