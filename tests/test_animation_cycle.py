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

    def test_animation_loop_survives_window_drag_without_relearning(self):
        """Dragging a window with an active spinner must keep hitting RAM cache during & after the drag."""
        import math
        from shadow_viewer import ShadowViewer

        def render_window_with_spinner(wx, wy, angle_deg):
            img = np.full((360, 480, 4), (30, 34, 42, 255), dtype=np.uint8)
            # Subtle desktop grid so 2D translation is unambiguous
            img[::24, :, :3] = (40, 46, 56)
            img[:, ::24, :3] = (40, 46, 56)
            # Window body + title bar + interior UI lines
            ww, wh = 220, 160
            cv2.rectangle(img, (wx, wy), (wx + ww, wy + wh), (242, 245, 248, 255), -1)
            cv2.rectangle(img, (wx, wy), (wx + ww, wy + 26), (52, 84, 132, 255), -1)
            cv2.rectangle(img, (wx, wy), (wx + ww, wy + wh), (90, 100, 115, 255), 1)
            cv2.putText(img, "DeskLite App", (wx + 10, wy + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255, 255), 1)
            cv2.rectangle(img, (wx + 12, wy + 40), (wx + 110, wy + 140), (225, 232, 240, 255), -1)
            cv2.putText(img, "Status: OK", (wx + 18, wy + 70), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (40, 50, 65, 255), 1)
            cv2.putText(img, "Frames: 60", (wx + 18, wy + 100), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (40, 50, 65, 255), 1)
            # 8-phase spinner inside window
            cx, cy = wx + 150, wy + 90
            cv2.circle(img, (cx, cy), 18, (220, 226, 234, 255), 3)
            rad = math.radians(angle_deg)
            ex = int(round(cx + 15 * math.cos(rad)))
            ey = int(round(cy + 15 * math.sin(rad)))
            cv2.line(img, (cx, cy), (ex, ey), (240, 110, 30, 255), 3)
            return img

        pipeline = VisionHostPipeline()
        pipeline.shared["debug_mode"] = False
        pipeline.shared["shadow_repair_active"] = True
        shadow = ShadowViewer()

        wx, wy = 80, 60
        # 1) Learn all 8 phases while window is stationary
        for i in range(24):
            f = render_window_with_spinner(wx, wy, (i % 8) * 45.0)
            pkt = pipeline.encode_frame(f, i == 0, ScreenCapturer._encode_jpeg, 75)
            out = shadow.process(f, pkt)
            self.assertIsNotNone(out)

        ram_before_drag = len(shadow.decoder.state_cache)
        repairs_before_drag = shadow.repaired_frames

        # 2) Drag window by odd/diagonal steps while spinner keeps rotating
        drag_anim_hits = 0
        for step_idx, (dx, dy) in enumerate([(7, 3), (5, 2), (-6, 3)], start=24):
            wx += dx
            wy += dy
            f = render_window_with_spinner(wx, wy, (step_idx % 8) * 45.0)
            pkt = pipeline.encode_frame(f, False, ScreenCapturer._encode_jpeg, 75)
            out = shadow.process(f, pkt)
            self.assertIsNotNone(out)
            self.assertIn(b'"window_move"', pkt)
            if b'"anim_cycle"' in pkt:
                drag_anim_hits += 1
            np.testing.assert_array_equal(shadow._pixels(), f[:, :, :3])

        self.assertGreaterEqual(drag_anim_hits, 2)

        # 3) Immediately after dropping at odd coords (86, 68), spinner must hit cache with 0 re-learning
        post_drag_hits = 0
        for i in range(27, 35):
            f = render_window_with_spinner(wx, wy, (i % 8) * 45.0)
            pkt = pipeline.encode_frame(f, False, ScreenCapturer._encode_jpeg, 75)
            out = shadow.process(f, pkt)
            self.assertIsNotNone(out)
            if pkt and (b'"anim_cycle"' in pkt or b'"bg_restore"' in pkt):
                post_drag_hits += 1
            np.testing.assert_array_equal(shadow._pixels(), f[:, :, :3])

        self.assertGreaterEqual(post_drag_hits, 7)
        self.assertLessEqual(shadow.repaired_frames, repairs_before_drag + 1)
        self.assertEqual(len(pipeline.shared.get("anim_hotspots", [])), 1)
        self.assertLessEqual(len(pipeline.shared["anim_hotspots"][0]["keyframes"]), 8)


if __name__ == "__main__":
    unittest.main()
