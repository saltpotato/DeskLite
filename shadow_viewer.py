"""Closed-loop host renderer: replays the actual viewer protocol before repair.

A single lossless PNG correction makes the displayed framebuffer agree with DXGI.
The shadow uses the real QImage/QPainter decoder to avoid approximating blits,
background restoration, cache state, and JPEG rounding in NumPy.
"""
import logging
import struct

import cv2
import numpy as np
from PySide6.QtGui import QImage

from vision_framework import VisionViewerDecoder, pack_jpeg_patch, pack_vf_packet


class ShadowViewer:
    def __init__(self):
        self.decoder = VisionViewerDecoder()
        self.canvas = None
        self.residual_bytes = 0
        self.repaired_frames = 0
        self.full_frames = 0

    def reset(self):
        self.decoder = VisionViewerDecoder()
        self.canvas = None

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

    def _new_keyframe(self, target):
        # An uncompressed prediction source is mandatory at the start of an epoch:
        # JPEG keyframes would otherwise introduce errors before the first delta.
        data = self._png(target)
        self.canvas = QImage.fromData(data)
        if self.canvas.isNull():
            raise ValueError("Could not decode keyframe")
        self.decoder.on_full_frame(len(data), self.canvas.width(),
                                   self.canvas.height(), self.canvas)
        self.full_frames += 1
        return data

    def process(self, target, packet):
        """Return a render-corrected wire packet, or None when no update exists.

        Entirely lossless at the frame boundary; thresholding is not used for
        residual detection. A single bounding PNG patch also repairs transparent
        windows, antialiasing, compositor shadows and incorrect movement guesses.
        """
        if target.ndim != 3 or target.shape[2] < 3:
            return packet
        if packet is None:
            # The motion recognizer may declare a frame unchanged even though
            # visually significant pixels were omitted by a confidence threshold.
            # Never leave those differences permanently stranded on the client.
            if self.canvas is None or self.canvas.isNull():
                return self._new_keyframe(target)
            if self.canvas.width() != target.shape[1] or self.canvas.height() != target.shape[0]:
                return self._new_keyframe(target)
            if not np.any(self._pixels() != target[:, :, :3]):
                return None
            packet = pack_vf_packet([])
        height, width = target.shape[:2]

        if (self.canvas is None or self.canvas.isNull()
                or self.canvas.width() != width or self.canvas.height() != height
                or not packet.startswith(b"VF")):
            return self._new_keyframe(target)

        # Only decode actual protocol operations; this mirrors the Qt viewer.
        self.decoder.apply_vf_packet(packet, self.canvas)
        reconstructed = self._pixels()
        bad = np.any(reconstructed != target[:, :, :3], axis=2)
        if not np.any(bad):
            return packet

        ys, xs = np.nonzero(bad)
        x1, x2 = int(xs.min()), int(xs.max()) + 1
        y1, y2 = int(ys.min()), int(ys.max()) + 1
        w, h = x2 - x1, y2 - y1

        correction = self._png(target[y1:y2, x1:x2])
        cmd = pack_jpeg_patch(x1, y1, w, h, correction)
        original_count = packet[2]
        # One extra command fits in the one-byte compound command count.
        # Large or unrepresentable corrections use a lossless keyframe.
        if original_count >= 255 or w * h >= width * height * 0.75:
            return self._new_keyframe(target)

        corrected = packet[:2] + bytes([original_count + 1]) + packet[3:] + cmd
        self.decoder.apply_vf_packet(pack_vf_packet([cmd]), self.canvas)
        # Detect renderer-specific differences rather than assuming PNG draws correctly.
        if np.any(self._pixels() != target[:, :, :3]):
            logging.warning("Shadow viewer could not reconstruct exact frame; sending keyframe")
            return self._new_keyframe(target)
        self.residual_bytes += len(correction)
        self.repaired_frames += 1
        return corrected
