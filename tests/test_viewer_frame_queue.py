import collections
import asyncio
import json
import unittest

from host_service import HostService
from viewer_window import RemoteDesktopViewer


class _Signal:
    def __init__(self):
        self.emissions = 0

    def emit(self):
        self.emissions += 1


class _QueueHarness:
    _enqueue_frame = RemoteDesktopViewer._enqueue_frame

    def __init__(self, capacity=2):
        self.frame_queue = collections.deque(maxlen=capacity)
        self.awaiting_keyframe = False
        self.render_pending = False
        self.frame_ready_sig = _Signal()
        self.sent_inputs = []

    def send_input(self, event):
        self.sent_inputs.append(event)


class _Capturer:
    def __init__(self):
        self.resets = 0

    def reset_vision_state(self):
        self.resets += 1


class _HostHarness:
    _process_control_message = HostService._process_control_message

    def __init__(self):
        self.capturer = _Capturer()
        self.force_frame = False

    def _ensure_background_tasks(self):
        pass


class ViewerFrameQueueTests(unittest.TestCase):
    def test_queue_overflow_requests_keyframe_and_discards_deltas_until_received(self):
        viewer = _QueueHarness()
        viewer._enqueue_frame(b"VF\x00first")
        viewer._enqueue_frame(b"VF\x00second")
        viewer._enqueue_frame(b"VF\x00third")

        self.assertTrue(viewer.awaiting_keyframe)
        self.assertEqual(len(viewer.frame_queue), 0)
        self.assertEqual(viewer.sent_inputs, [{"event": "request_keyframe"}])

        viewer._enqueue_frame(b"DL\x00\x00")
        self.assertEqual(len(viewer.frame_queue), 0)

        viewer._enqueue_frame(b"\x89PNG\r\n\x1a\nkeyframe")
        self.assertFalse(viewer.awaiting_keyframe)
        self.assertEqual(list(viewer.frame_queue), [b"\x89PNG\r\n\x1a\nkeyframe"])

    def test_keyframe_clears_stale_queued_deltas(self):
        viewer = _QueueHarness()
        viewer._enqueue_frame(b"VF\x00delta")
        viewer._enqueue_frame(b"\xff\xd8keyframe")
        self.assertEqual(list(viewer.frame_queue), [b"\xff\xd8keyframe"])

    def test_resync_request_resets_host_encoder_epoch(self):
        host = _HostHarness()
        asyncio.run(host._process_control_message(json.dumps({
            "type": "input",
            "data": {"event": "request_keyframe"},
        })))
        self.assertEqual(host.capturer.resets, 1)
        self.assertTrue(host.force_frame)


if __name__ == "__main__":
    unittest.main()
