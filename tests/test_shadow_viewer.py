"""Integration tests for host-side Qt shadow reconstruction.

Run: python -m unittest discover -s tests -v
Requires numpy, opencv-python-headless, PySide6-Essentials.
"""
import unittest

import cv2
import numpy as np

from shadow_viewer import ShadowViewer
from vision_framework import pack_jpeg_patch, pack_transform_rect, pack_vf_packet


class ShadowViewerTests(unittest.TestCase):
    def setUp(self):
        self.shadow = ShadowViewer()
        self.original = np.zeros((64, 96, 3), dtype=np.uint8)
        self.original[:, :, :] = (25, 40, 60)
        self.original[8:30, 10:45] = (235, 45, 120)

    def assert_synced(self, target):
        np.testing.assert_array_equal(self.shadow._pixels(), target)

    def test_initial_keyframe_is_lossless(self):
        packet = self.shadow.process(self.original, b"JPEG")
        self.assertTrue(packet.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assert_synced(self.original)

    def test_imperfect_motion_uses_lossless_residual(self):
        self.shadow.process(self.original, b"JPEG")
        target = np.roll(self.original, 3, axis=1)
        target[20, 20] = (10, 210, 14)  # Change within the moved window.
        packet = pack_vf_packet([pack_transform_rect(0, 0, 90, 64, 3, 0, 90, 64)])
        corrected = self.shadow.process(target, packet)
        self.assertTrue(corrected.startswith(b"VF"))
        self.assertEqual(corrected[2], 2)
        self.assert_synced(target)

    def test_lossy_jpeg_and_translucency_are_corrected(self):
        self.shadow.process(self.original, b"JPEG")
        target = self.original.copy()
        # Deliberately include a complex blended-looking surface and text-like edges.
        target[4:45, 15:80] = (
            (target[4:45, 15:80].astype(np.uint16) * 3 +
             np.array([30, 180, 220], dtype=np.uint16)) // 4
        ).astype(np.uint8)
        target[12:14, 16:75] = (245, 245, 245)
        ok, jpg = cv2.imencode(".jpg", target, [cv2.IMWRITE_JPEG_QUALITY, 35])
        self.assertTrue(ok)
        proposal = pack_vf_packet([pack_jpeg_patch(0, 0, 96, 64, jpg.tobytes())])
        repaired = self.shadow.process(target, proposal)
        self.assertTrue(repaired.startswith(b"VF") or repaired.startswith(b"\x89PNG"))
        self.assert_synced(target)

    def test_restart_does_not_reuse_old_canvas(self):
        self.shadow.process(self.original, b"JPEG")
        self.shadow.reset()
        target = 255 - self.original
        self.assertTrue(self.shadow.process(target, b"VF\x00").startswith(b"\x89PNG"))
        self.assert_synced(target)


if __name__ == "__main__":
    unittest.main()
