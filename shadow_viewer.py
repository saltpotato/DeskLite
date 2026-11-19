"""Closed-loop host renderer: replays the actual viewer protocol before repair.

A single lossless PNG correction makes the displayed framebuffer agree with DXGI.
The shadow uses the real QImage/QPainter decoder to avoid approximating blits,
background restoration, cache state, and JPEG rounding in NumPy.
"""
import logging
import struct
import time

import cv2
import numpy as np
from PySide6.QtGui import QImage

from vision_framework import (
    CMD_RESTORE_BG,
    CMD_RESTORE_STATE,
    CMD_RESTORE_SUBRECT,
    CMD_TRANSFORM_RECT,
    VisionViewerDecoder,
    pack_jpeg_patch,
    pack_vf_packet,
)


class ShadowViewer:
    def __init__(self, smart_mode: bool = False, settle_delay: float = 0.18, jpeg_motion_thresh: int = 10):
        self.decoder = VisionViewerDecoder()
        self.canvas = None
        self._pixel_image = None
        self.residual_bytes = 0
        self.repaired_frames = 0
        self.full_frames = 0
        self.smart_mode = bool(smart_mode)
        self.settle_delay = float(settle_delay)
        self.jpeg_motion_thresh = int(jpeg_motion_thresh)
        self._pending_settle_ts = 0.0

    def reset(self):
        self.decoder = VisionViewerDecoder()
        self.canvas = None
        self._pixel_image = None
        self._pending_settle_ts = 0.0

    def needs_settle(self, now: float = 0.0) -> bool:
        if not self.smart_mode or self._pending_settle_ts <= 0.0:
            return False
        if now <= 0.0:
            now = time.time()
        return (now - self._pending_settle_ts) >= self.settle_delay

    @staticmethod
    def _png(bgr):
        ok, encoded = cv2.imencode(".png", np.ascontiguousarray(bgr[:, :, :3]),
                                  [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise ValueError("PNG encoder failed")
        return encoded.tobytes()

    def _pixels(self):
        img = self.canvas.convertToFormat(QImage.Format.Format_RGB888)
        h, w = img.height(), img.width()
        raw = np.frombuffer(img.bits(), np.uint8).reshape(h, img.bytesPerLine())
        return raw[:, :w * 3].reshape(h, w, 3)[:, :, ::-1].copy()

    def _pixel_view(self):
        self._pixel_image = self.canvas
        img = self._pixel_image
        h, w = img.height(), img.width()
        raw = np.frombuffer(img.bits(), np.uint8).reshape(h, img.bytesPerLine())
        return raw[:, :w * 4].reshape(h, w, 4)

    @staticmethod
    def _absdiff_u8(left, right):
        if left.shape[:2] != right.shape[:2]:
            return None
        if left.size == 0:
            return np.zeros((0, 0, 3), dtype=np.uint8)
        if left.shape[2] == 4 and right.shape[2] == 3:
            left = left[:, :, :3]
        elif left.shape[2] != right.shape[2]:
            return None
        return cv2.absdiff(left, right)

    @staticmethod
    def _bad_u8_from_diff(diff, thresh: int = 0):
        if diff.size == 0:
            return np.zeros(diff.shape[:2], dtype=np.uint8)
        if diff.shape[2] >= 4:
            return cv2.bitwise_not(cv2.inRange(diff, (0, 0, 0, 0), (thresh, thresh, thresh, 255)))
        return cv2.bitwise_not(cv2.inRange(diff, (0, 0, 0), (thresh, thresh, thresh)))

    @staticmethod
    def _difference_mask(left, right, thresh: int = 0):
        diff = ShadowViewer._absdiff_u8(left, right)
        if diff is None:
            return None
        return ShadowViewer._bad_u8_from_diff(diff, thresh=thresh) != 0

    @staticmethod
    def _packet_uses_prediction(packet: bytes) -> bool:
        if not packet or len(packet) < 4 or not packet.startswith(b"VF"):
            return False
        count = packet[2]
        offset = 3
        n = len(packet)
        for _ in range(count):
            if offset >= n:
                break
            cmd = packet[offset]
            offset += 1
            if cmd in (CMD_RESTORE_STATE, CMD_TRANSFORM_RECT, CMD_RESTORE_BG, CMD_RESTORE_SUBRECT):
                return True
            if cmd == 0x01 and offset + 16 <= n:
                offset += 16
            elif cmd == 0x04 and offset + 9 <= n:
                offset += 9
            elif cmd == 0x05 and offset + 12 <= n:
                jlen, = struct.unpack_from(">I", packet, offset + 8)
                offset += 12 + jlen
            elif cmd == 0x06 and offset + 4 <= n:
                mlen, = struct.unpack_from(">I", packet, offset)
                offset += 4 + mlen
            elif cmd == 0x07 and offset + 8 <= n:
                offset += 8
            else:
                break
        return False

    def _new_keyframe(self, target):
        # An uncompressed prediction source is mandatory at the start of an epoch:
        # JPEG keyframes would otherwise introduce errors before the first delta.
        data = self._png(target)
        self.canvas = QImage.fromData(data).convertToFormat(QImage.Format.Format_ARGB32)
        self._pixel_image = None
        self._pending_settle_ts = 0.0
        if self.canvas.isNull():
            raise ValueError("Could not decode keyframe")
        self.decoder.on_full_frame(len(data), self.canvas.width(),
                                   self.canvas.height(), self.canvas)
        self.full_frames += 1
        return data

    def process(self, target, packet):
        """Return a render-corrected wire packet, or None when no update exists.

        In exact mode (`smart_mode=False`), repairs every non-zero pixel immediately.
        In `smart_mode=True`, strictly repairs motion/cache predictions immediately while
        deferring minor JPEG rounding noise until the region settles (~180ms).
        """
        if target.ndim != 3 or target.shape[2] < 3:
            return packet
        target = np.ascontiguousarray(target[:, :, :4] if target.shape[2] >= 4
                                      else target[:, :, :3])
        if packet is None:
            if self.canvas is None or self.canvas.isNull():
                return self._new_keyframe(target)
            if self.canvas.width() != target.shape[1] or self.canvas.height() != target.shape[0]:
                return self._new_keyframe(target)
            if self.smart_mode:
                if not self.needs_settle():
                    return None
                self._pending_settle_ts = 0.0
            pixels = self._pixel_view()
            idle_diff = self._absdiff_u8(pixels, target)
            if idle_diff is not None and cv2.countNonZero(self._bad_u8_from_diff(idle_diff, thresh=0)) == 0:
                return None
            del pixels
            self._pixel_image = None
            packet = pack_vf_packet([])
        height, width = target.shape[:2]

        if (self.canvas is None or self.canvas.isNull()
                or self.canvas.width() != width or self.canvas.height() != height
                or not packet.startswith(b"VF")):
            return self._new_keyframe(target)

        # Only decode actual protocol operations; this mirrors the Qt viewer.
        self._pixel_image = None
        self.decoder.apply_vf_packet(packet, self.canvas)
        reconstructed = self._pixel_view()

        use_motion_thresh = self.smart_mode and packet[2] > 0 and not self._packet_uses_prediction(packet)
        diff = self._absdiff_u8(reconstructed, target)
        if diff is None:
            return self._new_keyframe(target)
        if use_motion_thresh:
            bad_u8 = self._bad_u8_from_diff(diff, thresh=self.jpeg_motion_thresh)
            if cv2.countNonZero(bad_u8) == 0:
                if cv2.countNonZero(self._bad_u8_from_diff(diff, thresh=0)) > 0:
                    self._pending_settle_ts = time.time()
                else:
                    self._pending_settle_ts = 0.0
                return packet
        else:
            bad_u8 = self._bad_u8_from_diff(diff, thresh=0)
            if cv2.countNonZero(bad_u8) == 0:
                self._pending_settle_ts = 0.0
                return packet

        x1, y1, w, h = cv2.boundingRect(bad_u8)
        x2, y2 = x1 + w, y1 + h

        correction = self._png(target[y1:y2, x1:x2])
        cmd = pack_jpeg_patch(x1, y1, w, h, correction)
        original_count = packet[2]
        # One extra command fits in the one-byte compound command count.
        # Large or unrepresentable corrections use a lossless keyframe.
        if original_count >= 255 or w * h >= width * height * 0.75:
            return self._new_keyframe(target)

        corrected = packet[:2] + bytes([original_count + 1]) + packet[3:] + cmd
        del reconstructed
        self._pixel_image = None
        self.decoder.apply_vf_packet(pack_vf_packet([cmd]), self.canvas)
        # Detect renderer-specific differences rather than assuming PNG draws correctly.
        post_diff = self._absdiff_u8(self._pixel_view(), target)
        has_post_err = (post_diff is None) or (cv2.countNonZero(self._bad_u8_from_diff(post_diff, thresh=0)) > 0)
        if not use_motion_thresh and has_post_err:
            logging.warning("Shadow viewer could not reconstruct exact frame; sending keyframe")
            return self._new_keyframe(target)
        if use_motion_thresh and has_post_err:
            self._pending_settle_ts = time.time()
        else:
            self._pending_settle_ts = 0.0
        self.residual_bytes += len(correction)
        self.repaired_frames += 1
        return corrected
