import os
import sys
import unittest

import cv2
import numpy as np
from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shadow_viewer import ShadowViewer
from vision_framework import (
    CMD_RESTORE_SUBRECT,
    FrameContext,
    Scroll2DAndSliderRecognizer,
    VisionHostPipeline,
    pack_vf_packet,
)


_APP = QApplication.instance() or QApplication([])


class ScrollSubrectCacheTests(unittest.TestCase):
    def test_variable_step_scroll_back_uses_subrect_cache_hits(self):
        panel = np.full((800, 400, 3), 248, dtype=np.uint8)
        for row in range(0, 800, 30):
            color = (60 + (row * 17) % 150, 90 + (row * 29) % 140, 120 + (row * 43) % 120)
            cv2.putText(
                panel,
                f"Line #{row // 30:03d} - val={(row * 7) % 997} end-of-line-txt",
                (12, row + 20),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                color,
                1,
            )

        def make_frame(off):
            f = np.full((480, 640, 4), 20, dtype=np.uint8)
            f[:, :, 3] = 255
            f[60:380, 100:482, :3] = panel[off:off + 320, :382]
            f[60:380, 482:500, :3] = 220
            ty = 65 + int(off * 0.4)
            f[ty:ty + 40, 485:497, :3] = 100
            return f

        def enc(arr, q):
            _, b = cv2.imencode(".jpg", arr, [int(cv2.IMWRITE_JPEG_QUALITY), q])
            return b.tobytes()

        pipe = VisionHostPipeline()
        pipe.shared["shadow_repair_active"] = True
        shadow = ShadowViewer(smart_mode=True)
        rec = Scroll2DAndSliderRecognizer()

        f0 = make_frame(0)
        shadow.process(f0, pipe.encode_frame(f0, True, enc, 85))
        prev = f0.copy()

        subrect_frames = 0
        for off in [40, 82, 125, 80, 38]:
            fx = make_frame(off)
            ctx = FrameContext(prev, fx, (100, 60, 400, 320), enc, 85, pipe.shared)
            cmds = rec.analyze(ctx)
            self.assertIsNotNone(cmds)
            if any(c[0] == CMD_RESTORE_SUBRECT for c in cmds):
                subrect_frames += 1
            out = shadow.process(fx, pack_vf_packet(cmds))
            self.assertIsNotNone(out)
            np.testing.assert_array_equal(shadow._pixels(), fx[:, :, :3])
            prev = fx.copy()

        self.assertGreaterEqual(subrect_frames, 2)
        self.assertGreater(shadow.decoder.stats_cache_hits, 0)


if __name__ == "__main__":
    unittest.main()
