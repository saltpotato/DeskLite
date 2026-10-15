import unittest
from unittest.mock import patch

import numpy as np

from capturer import ScreenCapturer
from vision_framework import VisionHostPipeline


class CaptureModeTests(unittest.TestCase):
    def test_raw_capture_mode_encodes_full_frame_without_vision_pipeline(self):
        capturer = ScreenCapturer.__new__(ScreenCapturer)
        capturer.vision_enabled = False
        capturer.check_hot_reload = lambda now: None
        capturer._grab_frame_array = lambda force, now: (
            np.zeros((32, 48, 4), dtype=np.uint8), True
        )
        capturer.last_send_time = 0
        capturer.scale = 1.0
        capturer.quality = 75
        capturer.vision_pipeline = None
        capturer.shadow_viewer = None

        with patch.object(ScreenCapturer, "_encode_jpeg", return_value=b"jpeg") as encode:
            result = capturer.capture_frame(force=True)

        self.assertEqual(result, b"jpeg")
        encode.assert_called_once()

    def test_vision_recognizers_can_be_selected_individually(self):
        pipeline = VisionHostPipeline()
        pipeline.set_enabled_plugins(["WindowMove2DRecognizer"])
        self.assertEqual([plugin.name for plugin in pipeline.plugins],
                         ["WindowMove2DRecognizer"])
        pipeline.set_enabled_plugins([])
        self.assertEqual(pipeline.plugins, [])

    def test_shadow_repair_is_automatic_when_vision_is_enabled(self):
        capturer = ScreenCapturer.__new__(ScreenCapturer)
        capturer.vision_enabled = False
        capturer.vision_algorithms = None
        capturer.vision_pipeline = None
        capturer.shadow_viewer = None
        capturer.last_grid = None
        capturer.reset_vision_state = lambda: None

        capturer.update_processing_settings(vision_enabled=True)
        self.assertIsNotNone(capturer.shadow_viewer)

        capturer.update_processing_settings(vision_enabled=False)
        self.assertIsNone(capturer.shadow_viewer)


if __name__ == "__main__":
    unittest.main()
