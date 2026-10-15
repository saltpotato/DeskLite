"""Regression tests for frame dirtiness and JPEG prediction epoch resets."""
import unittest
import numpy as np
from vision_framework import VisionHostPipeline, VisionViewerDecoder


class VisionFrameResyncTests(unittest.TestCase):
    @staticmethod
    def jpeg_stub(frame, quality):
        # These tests exercise host state decisions, not JPEG fidelity.
        return b"encoded-frame"

    def test_subgrid_change_is_detected(self):
        pipeline = VisionHostPipeline()
        a = np.zeros((128, 128, 3), dtype=np.uint8)
        b = a.copy()
        b[5, 6] = [255, 255, 255]  # Not a 4x4 sampling origin.
        self.assertEqual(pipeline.encode_frame(a, False, self.jpeg_stub, 75),
                         b"encoded-frame")
        self.assertIsNotNone(pipeline.encode_frame(b, False, self.jpeg_stub, 75))

    def test_odd_dimensions_and_edge_pixel(self):
        pipeline = VisionHostPipeline()
        a = np.zeros((129, 131, 3), dtype=np.uint8)
        b = a.copy()
        b[-1, -1] = 255
        pipeline.encode_frame(a, False, self.jpeg_stub, 75)
        self.assertIsNotNone(pipeline.encode_frame(b, False, self.jpeg_stub, 75))

    def test_keyframe_resets_host_prediction_state(self):
        pipeline = VisionHostPipeline()
        pipeline._keyframe_interval = 1
        a = np.zeros((128, 128, 3), dtype=np.uint8)
        b = a.copy()
        b[10, 10] = 255
        c = b.copy()
        c[20, 20] = 255
        pipeline.encode_frame(a, False, self.jpeg_stub, 75)
        self.assertIsNotNone(pipeline.encode_frame(b, False, self.jpeg_stub, 75))
        pipeline.shared["known_states"][123] = (0, 0, 10, 10)
        self.assertEqual(pipeline.encode_frame(c, False, self.jpeg_stub, 75),
                         b"encoded-frame")
        self.assertFalse(pipeline.shared["known_states"])
        self.assertEqual(pipeline._delta_frames_since_keyframe, 0)


if __name__ == "__main__":
    unittest.main()
