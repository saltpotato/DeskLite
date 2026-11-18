"""Regression tests for 2D window dragging with rounded corners and drop shadows."""

import unittest
import cv2
import numpy as np

from capturer import ScreenCapturer
from shadow_viewer import ShadowViewer
from vision_framework import VisionHostPipeline


def _render_window_frame(win_x: int, win_y: int) -> np.ndarray:
    """Render a 640x480 textured desktop with a shadowed, rounded-like window."""
    img = np.full((480, 640, 3), 24, dtype=np.uint8)
    for gx in range(0, 640, 40):
        img[:, gx] = 42
    for gy in range(0, 480, 40):
        img[gy, :] = 42

    w, h = 280, 220
    # Drop shadow around window border
    img[win_y + 4 : win_y + h + 6, win_x + 4 : win_x + w + 6] = np.clip(
        img[win_y + 4 : win_y + h + 6, win_x + 4 : win_x + w + 6].astype(np.int16) - 10,
        0, 255
    ).astype(np.uint8)

    # Window body + titlebar text
    cv2.rectangle(img, (win_x, win_y), (win_x + w, win_y + h), (242, 244, 248), -1)
    cv2.rectangle(img, (win_x, win_y), (win_x + w, win_y + 30), (30, 41, 59), -1)
    cv2.putText(img, "Test Window", (win_x + 12, win_y + 21),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (240, 245, 250), 1)
    for row in range(4):
        cv2.putText(img, f"Line {row}: sample window content",
                    (win_x + 14, win_y + 60 + row * 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.48, (30, 41, 59), 1)
    return img


class WindowMoveTests(unittest.TestCase):
    def test_window_drag_uses_gpu_move_without_full_shadow_repair(self):
        """Dragging a shadowed window across a textured backdrop must emit window_move with 0 shadow repairs."""
        pipeline = VisionHostPipeline()
        pipeline.shared["debug_mode"] = False
        pipeline.shared["shadow_repair_active"] = True
        shadow = ShadowViewer()

        f0 = _render_window_frame(80, 60)
        p0 = pipeline.encode_frame(f0, False, ScreenCapturer._encode_jpeg, 75)
        shadow.process(f0, p0)

        move_hits = 0
        for step in range(1, 5):
            fx = _render_window_frame(80 + step * 20, 60 + step * 12)
            pkt = pipeline.encode_frame(fx, False, ScreenCapturer._encode_jpeg, 75)
            out = shadow.process(fx, pkt)
            self.assertIsNotNone(out)
            if pkt and b'"window_move"' in pkt:
                move_hits += 1

        self.assertEqual(move_hits, 4)
        self.assertEqual(shadow.repaired_frames, 0)

    def test_left_and_small_diagonal_window_drags(self):
        """Dragging left or by small shifts (-6, -3) must match right drags symmetrically."""
        pipeline = VisionHostPipeline()
        pipeline.shared["debug_mode"] = False
        pipeline.shared["shadow_repair_active"] = True
        shadow = ShadowViewer()

        f0 = _render_window_frame(240, 140)
        p0 = pipeline.encode_frame(f0, False, ScreenCapturer._encode_jpeg, 75)
        shadow.process(f0, p0)

        for dx, dy in [(-24, 0), (-6, -3), (-18, -10)]:
            cur_x, cur_y = 240 + dx, 140 + dy
            fx = _render_window_frame(cur_x, cur_y)
            pkt = pipeline.encode_frame(fx, False, ScreenCapturer._encode_jpeg, 75)
            shadow.process(fx, pkt)
            self.assertIsNotNone(pkt)
            self.assertIn(b'"window_move"', pkt)
            f0 = _render_window_frame(240, 140)
            p_reset = pipeline.encode_frame(f0, False, ScreenCapturer._encode_jpeg, 75)
            shadow.process(f0, p_reset)

        self.assertEqual(shadow.repaired_frames, 0)


if __name__ == "__main__":
    unittest.main()
