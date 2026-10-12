"""
DeskLite Modular Vision & Spatial Memory Framework (`vision_framework.py`)
==========================================================================
Provides a decoupled, pluggable architecture between DeskLite's low-level
ScreenCapturer/Viewer and high-level Vision/AI/Caching algorithms.

Wire Protocol (`b"VF"` compound packet):
  Header: b"VF" + uint8(num_commands)
  Commands:
    0x01 CMD_SAVE_STATE:     <QHHHH (hash64, sx, sy, sw, sh)              [17 bytes]
    0x02 CMD_RESTORE_STATE:  <QHHHH (hash64, dx, dy, dw, dh)              [17 bytes]
    0x03 CMD_TRANSFORM_RECT: <HHHHHHHH (sx, sy, sw, sh, dx, dy, dw, dh)   [17 bytes]
    0x04 CMD_MARK_DIRTY:     <QB (hash64, direction_flags)                [10 bytes]
    0x05 CMD_JPEG_PATCH:     <HHHHI (dx, dy, dw, dh, length) + jpeg_bytes [13 + N bytes]
    0x06 CMD_OVERLAY_META:   <I (length) + utf8_json_bytes                [5 + N bytes]
"""

import collections
import json
import struct
import time
import zlib
import numpy as np

CMD_SAVE_STATE = 0x01
CMD_RESTORE_STATE = 0x02
CMD_TRANSFORM_RECT = 0x03
CMD_MARK_DIRTY = 0x04
CMD_JPEG_PATCH = 0x05
CMD_OVERLAY_META = 0x06


def fast_region_hash(arr: np.ndarray) -> int:
    """Computes an ultra-fast 64-bit content fingerprint of a 3D BGR/BGRA numpy region."""
    h, w = arr.shape[:2]
    if h < 4 or w < 4:
        return 0
    # Subsample every 4th pixel in X and Y for <0.15ms hashing even on 1080p regions
    sub = np.ascontiguousarray(arr[::4, ::4, :3])
    c1 = zlib.adler32(sub.data) & 0xFFFFFFFF
    c2 = zlib.crc32(sub.data) & 0xFFFFFFFF
    # Mix dimensions into the 64-bit hash so differently sized regions never collide
    dim_mix = ((w & 0xFFFF) << 16) | (h & 0xFFFF)
    return ((c1 ^ dim_mix) << 32) | c2


# ============================================================================
# 1. HOST-SIDE PLUGGABLE RECOGNIZER INTERFACE
# ============================================================================

class FrameContext:
    """Passed to each registered Recognizer plugin on the Host."""
    def __init__(self, prev_frame: np.ndarray, curr_frame: np.ndarray, dirty_rect: tuple,
                 encode_jpeg_fn, quality: int, shared_state: dict):
        self.prev_frame = prev_frame
        self.curr_frame = curr_frame
        self.dirty_rect = dirty_rect  # (x, y, w, h)
        self.encode_jpeg = encode_jpeg_fn
        self.quality = quality
        self.shared = shared_state


class BaseRecognizer:
    """
    Base class for all DeskLite Vision / AI / Compression plugins.
    Subclass this and register via `pipeline.register_plugin(MyPlugin())`.
    """
    name = "BaseRecognizer"

    def on_input_event(self, evt: dict, shared: dict):
        """Optional hook called when user/AI input (scroll, click, key) arrives."""
        pass

    def analyze(self, ctx: FrameContext):
        """
        Inspects `ctx.prev_frame` and `ctx.curr_frame` within `ctx.dirty_rect`.
        Returns a list of encoded binary command chunks if handled, or None to pass
        to the next plugin in the pipeline.
        """
        return None


class StateCacheRecognizer(BaseRecognizer):
    """
    Recognizes repeated UI states:
    - Switching between Browser / IDE Tabs (Reiterkarten)
    - Selecting / Deselecting items, Hover states, Dropdown open/close, Blinking cursors
    - Returning to a previously seen scroll position
    Saves 100% of JPEG image transfer (sends 17-byte CMD_RESTORE_STATE instead).
    """
    name = "StateCacheRecognizer"

    def analyze(self, ctx: FrameContext):
        x, y, w, h = ctx.dirty_rect
        if w < 16 or h < 16:
            return None

        prev_roi = ctx.prev_frame[y:y + h, x:x + w]
        curr_roi = ctx.curr_frame[y:y + h, x:x + w]

        prev_hash = fast_region_hash(prev_roi)
        curr_hash = fast_region_hash(curr_roi)
        if prev_hash == 0 or curr_hash == 0 or prev_hash == curr_hash:
            return None

        known: collections.OrderedDict = ctx.shared["known_states"]
        dirty_set: set = ctx.shared["dirty_states"]
        cmds = []

        # 1. Queue saving the state currently on the Viewer's screen BEFORE overwriting it
        if prev_hash not in known:
            ctx.shared["pending_pre_save"] = (prev_hash, x, y, w, h)

        # 2. Check if the new screen region is ALREADY in the Viewer's RAM cache (and not dirty)!
        if curr_hash in known and curr_hash not in dirty_set:
            pre = ctx.shared.get("pending_pre_save")
            if pre:
                phash, px, py, pw, ph = pre
                cmds.append(pack_save_state(phash, px, py, pw, ph))
                self._commit_known(known, dirty_set, phash, (px, py, pw, ph))
                ctx.shared["pending_pre_save"] = None

            known.move_to_end(curr_hash)
            cmds.append(pack_restore_state(curr_hash, x, y, w, h))
            est_saved = max(1024, int(w * h * 0.12))
            ctx.shared["bytes_saved"] += est_saved
            cmds.append(pack_overlay_meta({
                "event": "cache_hit",
                "rect": [x, y, w, h],
                "saved_kb": round(ctx.shared["bytes_saved"] / 1024.0, 1)
            }))
            ctx.prev_frame[y:y + h, x:x + w] = curr_roi
            return cmds

        ctx.shared["pending_post_save"] = (curr_hash, x, y, w, h)
        return None

    @staticmethod
    def _commit_known(known: collections.OrderedDict, dirty_set: set, shash: int, rect: tuple):
        known[shash] = rect
        known.move_to_end(shash)
        dirty_set.discard(shash)
        while len(known) > 96:
            evicted, _ = known.popitem(last=False)
            dirty_set.discard(evicted)


class Scroll2DAndSliderRecognizer(BaseRecognizer):
    """
    Detects 2D scrolling (vertical Δy and horizontal Δx) + monitors vertical/horizontal
    scrollbar sliders to mark offscreen cached content as DIRTY when a document grows!
    Includes closed-loop residual patching so stationary UI elements (sticky headers,
    sidebars, borders, cursors) inside a scrolling bounding box never smear.
    """
    name = "Scroll2DAndSliderRecognizer"

    def analyze(self, ctx: FrameContext):
        x, y, w, h = ctx.dirty_rect
        if w < 160 or h < 120:
            return None

        prev_roi = ctx.prev_frame[y:y + h, x:x + w]
        curr_roi = ctx.curr_frame[y:y + h, x:x + w]

        # Separate inner content area from potential right/bottom scrollbar tracks (~18px)
        sb_w = min(20, max(12, w // 25))
        inner_w = w - sb_w
        inner_h = h - sb_w

        # 1. Detect vertical shift (Δy) using fast 1D row-luminance profile correlation
        dy = self._detect_1d_shift(
            prev_roi[:inner_h, :inner_w, 1],
            curr_roi[:inner_h, :inner_w, 1],
            axis=1,
            max_shift=min(h // 2, 360)
        )

        # 2. Detect horizontal shift (Δx) if vertical shift is 0
        dx = 0
        if dy == 0:
            dx = self._detect_1d_shift(
                prev_roi[:inner_h, :inner_w, 1],
                curr_roi[:inner_h, :inner_w, 1],
                axis=0,
                max_shift=min(w // 2, 360)
            )

        # 3. Check vertical scrollbar strip (right edge) for offscreen content growth (DIRTY check)
        slider_changed = self._check_scrollbar_dirty(prev_roi[:, inner_w:], curr_roi[:, inner_w:])
        if dy == 0 and dx == 0:
            if slider_changed:
                ctx.shared["dirty_below"] = True
                ctx.shared["pending_cmds"].append(pack_mark_dirty(0, 1))
                ctx.shared["pending_cmds"].append(pack_overlay_meta({
                    "event": "dirty_below",
                    "viewport": [x, y, w, h],
                    "virtual_y": ctx.shared.get("virtual_y", 0),
                    "dirty_below": True,
                    "saved_kb": round(ctx.shared["bytes_saved"] / 1024.0, 1)
                }))
            return None

        # Verify that shifting prev_roi by (dx, dy) genuinely matches curr_roi in the overlap region!
        if not self._verify_overlap(prev_roi, curr_roi, dx, dy, inner_w, inner_h):
            return None

        if dy != 0:
            sh_abs = abs(dy)
            overlap_h = h - sh_abs

            if dy > 0:
                src_y, dst_y = y + dy, y
                new_y = y + overlap_h
                out_y = y
            else:
                src_y, dst_y = y, y + sh_abs
                new_y = y
                out_y = y + h - sh_abs

            # Check residual differences inside the shifted overlap (sticky headers, sidebars, borders)
            shifted_overlap = ctx.prev_frame[src_y:src_y + overlap_h, x:x + inner_w].copy()
            target_overlap = ctx.curr_frame[dst_y:dst_y + overlap_h, x:x + inner_w]
            residual_rects = self._find_residual_rects(shifted_overlap, target_overlap, x, dst_y)
            if residual_rects is None:
                return None

            cmds = list(ctx.shared["pending_cmds"])
            ctx.shared["pending_cmds"].clear()
            pre = ctx.shared.get("pending_pre_save")
            if pre:
                phash, px, py, pw, ph = pre
                cmds.append(pack_save_state(phash, px, py, pw, ph))
                StateCacheRecognizer._commit_known(ctx.shared["known_states"], ctx.shared["dirty_states"], phash, (px, py, pw, ph))
                ctx.shared["pending_pre_save"] = None
            ctx.shared["pending_post_save"] = None

            ctx.shared["virtual_y"] = ctx.shared.get("virtual_y", 0) + dy
            ctx.shared["viewport"] = [x, y, w, h]

            # 1) Save outgoing strip into Viewer RAM cache BEFORE shifting over it
            out_roi = ctx.prev_frame[out_y:out_y + sh_abs, x:x + w]
            out_hash = fast_region_hash(out_roi)
            if out_hash and out_hash not in ctx.shared["known_states"]:
                cmds.append(pack_save_state(out_hash, x, out_y, w, sh_abs))
                StateCacheRecognizer._commit_known(ctx.shared["known_states"], ctx.shared["dirty_states"], out_hash, (x, out_y, w, sh_abs))

            # 2) Shift overlapping body locally on the Viewer GPU (0 KB image data!)
            cmds.append(pack_transform_rect(
                x, src_y, inner_w, overlap_h,
                x, dst_y, inner_w, overlap_h
            ))
            ctx.prev_frame[dst_y:dst_y + overlap_h, x:x + inner_w] = shifted_overlap

            # 3) Encode newly revealed strip (or restore from Viewer RAM cache if seen before)
            new_strip = ctx.curr_frame[new_y:new_y + sh_abs, x:x + w]
            cmds.extend(self._encode_or_restore_strip(ctx, new_strip, x, new_y, w, sh_abs))
            ctx.prev_frame[new_y:new_y + sh_abs, x:x + w] = new_strip

            # 4) Encode narrow right scrollbar track
            sb_strip = ctx.curr_frame[dst_y:dst_y + overlap_h, x + inner_w:x + w]
            cmds.append(pack_jpeg_patch(x + inner_w, dst_y, sb_w, overlap_h, ctx.encode_jpeg(sb_strip, ctx.quality)))
            ctx.prev_frame[dst_y:dst_y + overlap_h, x + inner_w:x + w] = sb_strip

            # 5) Patch any stationary/sticky UI sub-rectangles inside the overlap so nothing ever smears!
            for rx, ry, rw, rh in residual_rects:
                res_patch = ctx.curr_frame[ry:ry + rh, rx:rx + rw]
                cmds.append(pack_jpeg_patch(rx, ry, rw, rh, ctx.encode_jpeg(res_patch, ctx.quality)))
                ctx.prev_frame[ry:ry + rh, rx:rx + rw] = res_patch

            # 6) Save the complete post-scroll viewport state in Viewer RAM cache
            post_hash = fast_region_hash(ctx.curr_frame[y:y + h, x:x + w])
            if post_hash and post_hash not in ctx.shared["known_states"]:
                cmds.append(pack_save_state(post_hash, x, y, w, h))
                StateCacheRecognizer._commit_known(ctx.shared["known_states"], ctx.shared["dirty_states"], post_hash, (x, y, w, h))

            est_saved = max(512, int(inner_w * overlap_h * 0.10))
            ctx.shared["bytes_saved"] += est_saved
            cmds.append(pack_overlay_meta({
                "event": "scroll",
                "dx": dx,
                "dy": dy,
                "viewport": [x, y, w, h],
                "virtual_y": ctx.shared["virtual_y"],
                "dirty_below": ctx.shared.get("dirty_below", False),
                "saved_kb": round(ctx.shared["bytes_saved"] / 1024.0, 1)
            }))
            return cmds

        elif dx != 0:
            sw_abs = abs(dx)
            overlap_w = w - sw_abs
            if dx > 0:
                src_x, dst_x = x + dx, x
                new_x = x + overlap_w
            else:
                src_x, dst_x = x, x + sw_abs
                new_x = x

            shifted_overlap = ctx.prev_frame[y:y + h, src_x:src_x + overlap_w].copy()
            target_overlap = ctx.curr_frame[y:y + h, dst_x:dst_x + overlap_w]
            residual_rects = self._find_residual_rects(shifted_overlap, target_overlap, dst_x, y)
            if residual_rects is None:
                return None

            cmds = list(ctx.shared["pending_cmds"])
            ctx.shared["pending_cmds"].clear()
            pre = ctx.shared.get("pending_pre_save")
            if pre:
                phash, px, py, pw, ph = pre
                cmds.append(pack_save_state(phash, px, py, pw, ph))
                StateCacheRecognizer._commit_known(ctx.shared["known_states"], ctx.shared["dirty_states"], phash, (px, py, pw, ph))
                ctx.shared["pending_pre_save"] = None
            ctx.shared["pending_post_save"] = None

            ctx.shared["virtual_x"] = ctx.shared.get("virtual_x", 0) + dx
            cmds.append(pack_transform_rect(src_x, y, overlap_w, h, dst_x, y, overlap_w, h))
            ctx.prev_frame[y:y + h, dst_x:dst_x + overlap_w] = shifted_overlap

            new_strip = ctx.curr_frame[y:y + h, new_x:new_x + sw_abs]
            cmds.extend(self._encode_or_restore_strip(ctx, new_strip, new_x, y, sw_abs, h))
            ctx.prev_frame[y:y + h, new_x:new_x + sw_abs] = new_strip

            for rx, ry, rw, rh in residual_rects:
                res_patch = ctx.curr_frame[ry:ry + rh, rx:rx + rw]
                cmds.append(pack_jpeg_patch(rx, ry, rw, rh, ctx.encode_jpeg(res_patch, ctx.quality)))
                ctx.prev_frame[ry:ry + rh, rx:rx + rw] = res_patch

            est_saved = max(512, int(overlap_w * h * 0.10))
            ctx.shared["bytes_saved"] += est_saved
            cmds.append(pack_overlay_meta({
                "event": "scroll_x",
                "dx": dx,
                "dy": 0,
                "viewport": [x, y, w, h],
                "virtual_y": ctx.shared.get("virtual_y", 0),
                "saved_kb": round(ctx.shared["bytes_saved"] / 1024.0, 1)
            }))
            return cmds

        return None

    def _encode_or_restore_strip(self, ctx: FrameContext, strip_arr: np.ndarray, sx: int, sy: int, sw: int, sh: int) -> list:
        shash = fast_region_hash(strip_arr)
        known = ctx.shared["known_states"]
        dirty = ctx.shared["dirty_states"]
        if shash and shash in known and shash not in dirty:
            ctx.shared["bytes_saved"] += max(512, int(sw * sh * 0.12))
            known.move_to_end(shash)
            return [pack_restore_state(shash, sx, sy, sw, sh)]

        out = [pack_jpeg_patch(sx, sy, sw, sh, ctx.encode_jpeg(strip_arr, ctx.quality))]
        if shash:
            out.append(pack_save_state(shash, sx, sy, sw, sh))
            StateCacheRecognizer._commit_known(known, dirty, shash, (sx, sy, sw, sh))
        return out

    @staticmethod
    def _find_residual_rects(shifted: np.ndarray, target: np.ndarray, base_x: int, base_y: int):
        """
        Compares the shifted overlap region against the true target region on a 4x4 grid.
        Peels off stationary top/bottom headers and left/right borders into thin strips
        plus any interior residual box so L-shaped borders never inflate into full-screen boxes.
        Returns None if the mismatch area exceeds 32% of the overlap (abort scroll transform).
        """
        oh, ow = shifted.shape[:2]
        if oh < 8 or ow < 8:
            return []
        diff = np.abs(shifted[::4, ::4, :3].astype(np.int16) - target[::4, ::4, :3].astype(np.int16)).max(axis=2)
        bad = diff > 12
        bad_ratio = float(bad.mean())
        if bad_ratio > 0.32:
            return None
        if bad_ratio == 0.0:
            return []

        gh, gw = bad.shape
        r0, r1 = 0, gh
        c0, c1 = 0, gw
        rects = []

        # 1. Peel stationary top rows (e.g. top border or sticky header)
        while r0 < r1 and float(bad[r0, c0:c1].mean()) > 0.20:
            r0 += 1
        if r0 > 0:
            py2 = min(oh, r0 * 4 + 4)
            rects.append((base_x, base_y, ow, py2))

        # 2. Peel stationary bottom rows (e.g. bottom border or sticky footer)
        orig_r1 = r1
        while r1 > r0 and float(bad[r1 - 1, c0:c1].mean()) > 0.20:
            r1 -= 1
        if r1 < orig_r1:
            py1 = max(0, r1 * 4 - 4)
            rects.append((base_x, base_y + py1, ow, oh - py1))

        # 3. Peel stationary left columns (e.g. left border or sidebar)
        mid_y1 = min(oh, r0 * 4)
        mid_y2 = max(mid_y1, min(oh, r1 * 4))
        mid_h = mid_y2 - mid_y1
        if mid_h > 0 and r0 < r1:
            while c0 < c1 and float(bad[r0:r1, c0].mean()) > 0.20:
                c0 += 1
            if c0 > 0:
                px2 = min(ow, c0 * 4 + 4)
                rects.append((base_x, base_y + mid_y1, px2, mid_h))

            # 4. Peel stationary right columns
            orig_c1 = c1
            while c1 > c0 and float(bad[r0:r1, c1 - 1].mean()) > 0.20:
                c1 -= 1
            if c1 < orig_c1:
                px1 = max(0, c1 * 4 - 4)
                rects.append((base_x + px1, base_y + mid_y1, ow - px1, mid_h))

        # 5. Any remaining interior mismatch (e.g. floating button or mouse cursor)
        if r0 < r1 and c0 < c1:
            sub = bad[r0:r1, c0:c1]
            ys, xs = np.nonzero(sub)
            if ys.size > 0:
                iy1 = max(0, (r0 + int(ys.min())) * 4 - 4)
                iy2 = min(oh, (r0 + int(ys.max()) + 1) * 4 + 4)
                ix1 = max(0, (c0 + int(xs.min())) * 4 - 4)
                ix2 = min(ow, (c0 + int(xs.max()) + 1) * 4 + 4)
                if ix2 > ix1 and iy2 > iy1:
                    rects.append((base_x + ix1, base_y + iy1, ix2 - ix1, iy2 - iy1))

        total_area = sum(rw * rh for _, _, rw, rh in rects)
        if total_area > (ow * oh * 0.42):
            return None
        return rects

    @staticmethod
    def _detect_1d_shift(prev_g: np.ndarray, curr_g: np.ndarray, axis: int, max_shift: int) -> int:
        """
        Finds the pixel shift along `axis` (1=vertical dy, 0=horizontal dx) using
        1D gradient projections. Runs in <0.5ms in numpy.
        """
        p1 = prev_g.mean(axis=axis, dtype=np.float32)
        p2 = curr_g.mean(axis=axis, dtype=np.float32)
        d1 = np.diff(p1)
        d2 = np.diff(p2)
        n = len(d1)
        if n < 64 or float(np.std(d1)) < 0.8 or float(np.std(d2)) < 0.8:
            return 0

        margin = min(max_shift, n // 3)
        if margin < 8:
            return 0
        tpl = d2[margin:n - margin]
        tpl_norm = tpl - tpl.mean()
        tpl_energy = float(np.dot(tpl_norm, tpl_norm))
        if tpl_energy < 1e-3:
            return 0

        corr = np.correlate(d1 - d1.mean(), tpl_norm, mode="valid")
        best_idx = int(np.argmax(corr))
        shift = best_idx - margin  # >0 means content moved UP/LEFT by `shift` px
        if abs(shift) < 6:
            return 0

        seg = d1[best_idx:best_idx + len(tpl)]
        seg_norm = seg - seg.mean()
        denom = float(np.sqrt(np.dot(seg_norm, seg_norm) * tpl_energy))
        if denom <= 1e-5 or (float(corr[best_idx]) / denom) < 0.80:
            return 0
        return shift

    @staticmethod
    def _verify_overlap(prev_roi: np.ndarray, curr_roi: np.ndarray, dx: int, dy: int, w: int, h: int) -> bool:
        """Verifies that the shifted overlapping region genuinely matches on a coarse 8x8 pixel grid."""
        if dy > 0:
            a = prev_roi[dy:h:8, :w:8, :3]
            b = curr_roi[:h - dy:8, :w:8, :3]
        elif dy < 0:
            s = -dy
            a = prev_roi[:h - s:8, :w:8, :3]
            b = curr_roi[s:h:8, :w:8, :3]
        elif dx > 0:
            a = prev_roi[:h:8, dx:w:8, :3]
            b = curr_roi[:h:8, :w - dx:8, :3]
        else:
            s = -dx
            a = prev_roi[:h:8, :w - s:8, :3]
            b = curr_roi[:h:8, s:w:8, :3]

        if a.size == 0 or a.shape != b.shape:
            return False
        mad = float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())
        return mad < 6.0

    @staticmethod
    def _check_scrollbar_dirty(prev_sb: np.ndarray, curr_sb: np.ndarray) -> bool:
        """Checks if the vertical scrollbar track changed significantly while content stayed static."""
        if prev_sb.size == 0 or prev_sb.shape != curr_sb.shape:
            return False
        diff = np.abs(prev_sb[:, :, :3].astype(np.int16) - curr_sb[:, :, :3].astype(np.int16))
        changed_rows = (diff.max(axis=(1, 2)) > 18).sum()
        return int(changed_rows) >= 6


# ============================================================================
# 2. HOST PIPELINE MANAGER (Single Entry Point for `capturer.py`)
# ============================================================================

class VisionHostPipeline:
    """
    Lightweight coordinator hooked into `ScreenCapturer`.
    Runs registered Recognizer plugins and packs their output into a `b"VF"` frame.
    Maintains `self.prev_frame` in lockstep with the Viewer's reconstructed canvas.
    """
    def __init__(self):
        self.plugins: list[BaseRecognizer] = [
            StateCacheRecognizer(),
            Scroll2DAndSliderRecognizer(),
        ]
        self.prev_frame = None
        self.shared = {
            "known_states": collections.OrderedDict(),
            "dirty_states": set(),
            "pending_cmds": [],
            "pending_pre_save": None,
            "pending_post_save": None,
            "virtual_y": 0,
            "virtual_x": 0,
            "dirty_below": False,
            "bytes_saved": 0,
        }

    def register_plugin(self, plugin: BaseRecognizer):
        self.plugins.append(plugin)

    def reset(self):
        self.prev_frame = None
        self.shared["known_states"].clear()
        self.shared["dirty_states"].clear()
        self.shared["pending_cmds"].clear()
        self.shared["pending_pre_save"] = None
        self.shared["pending_post_save"] = None
        self.shared["virtual_y"] = 0
        self.shared["virtual_x"] = 0
        self.shared["dirty_below"] = False

    def notify_input(self, evt: dict):
        if evt.get("event") in ("key_down", "mouse_down"):
            self.shared["dirty_below"] = False
        for p in self.plugins:
            p.on_input_event(evt, self.shared)

    def encode_frame(self, curr_frame: np.ndarray, force: bool, encode_jpeg_fn, quality: int):
        """
        Single entry point called by `ScreenCapturer.capture_frame`.
        Detects the dirty bounding box, runs all registered Vision/Memory plugins,
        and returns either a `b"VF"` vision packet, a `b"DL"` delta JPEG, or a full keyframe.
        """
        fh, fw = curr_frame.shape[:2]
        if force or self.prev_frame is None or self.prev_frame.shape != curr_frame.shape:
            self.prev_frame = curr_frame.copy()
            return encode_jpeg_fn(curr_frame, quality)

        # Fast 8x8 grid diff to locate bounding box of screen changes
        g_prev = self.prev_frame[::8, ::8, :3]
        g_curr = curr_frame[::8, ::8, :3]
        mask = np.any(g_prev != g_curr, axis=2)
        ys, xs = np.nonzero(mask)
        if ys.size == 0:
            return None

        x1 = max(0, int(xs.min()) * 8)
        y1 = max(0, int(ys.min()) * 8)
        x2 = min(fw, (int(xs.max()) + 1) * 8)
        y2 = min(fh, (int(ys.max()) + 1) * 8)
        dirty_rect = (x1, y1, x2 - x1, y2 - y1)

        vf_packet = self.process_delta(self.prev_frame, curr_frame, dirty_rect, encode_jpeg_fn, quality)
        if vf_packet is not None:
            return vf_packet

        self.prev_frame = curr_frame.copy()
        w, h = dirty_rect[2], dirty_rect[3]
        if w * h >= fw * fh * 0.75:
            return encode_jpeg_fn(curr_frame, quality)

        patch_bytes = encode_jpeg_fn(curr_frame[y1:y2, x1:x2], quality)
        return b"DL" + struct.pack(">HHHH", x1, y1, w, h) + patch_bytes

    def process_delta(self, prev_frame: np.ndarray, curr_frame: np.ndarray,
                      dirty_rect: tuple, encode_jpeg_fn, quality: int):
        """
        Runs registered Recognizer plugins on `dirty_rect`.
        Returns a `b"VF"` packet if any plugin optimized the frame (or queued state commands),
        or `None` to fall back to the standard `b"DL"` delta JPEG.
        """
        self.shared["pending_cmds"].clear()
        self.shared["pending_pre_save"] = None
        self.shared["pending_post_save"] = None
        ctx = FrameContext(prev_frame, curr_frame, dirty_rect, encode_jpeg_fn, quality, self.shared)

        for plugin in self.plugins:
            try:
                result_cmds = plugin.analyze(ctx)
                if result_cmds:
                    return pack_vf_packet(result_cmds)
            except Exception:
                continue

        # If StateCacheRecognizer queued a pre/post save around a normal update (e.g. Tab switch),
        # bundle SAVE_STATE(prev) + JPEG_PATCH(curr) + SAVE_STATE(curr) into a VF packet!
        pre = self.shared.get("pending_pre_save")
        post = self.shared.get("pending_post_save")
        if self.shared["pending_cmds"] or pre or post:
            x, y, w, h = dirty_rect
            if w * h >= curr_frame.shape[0] * curr_frame.shape[1] * 0.75:
                return None
            roi = curr_frame[y:y + h, x:x + w]
            jpeg_bytes = encode_jpeg_fn(roi, quality)
            cmds = []
            if pre:
                phash, px, py, pw, ph = pre
                cmds.append(pack_save_state(phash, px, py, pw, ph))
                StateCacheRecognizer._commit_known(self.shared["known_states"], self.shared["dirty_states"], phash, (px, py, pw, ph))
                self.shared["pending_pre_save"] = None

            cmds.extend(self.shared["pending_cmds"])
            self.shared["pending_cmds"].clear()
            cmds.append(pack_jpeg_patch(x, y, w, h, jpeg_bytes))

            if post:
                chash, sx, sy, sw, sh = post
                cmds.append(pack_save_state(chash, sx, sy, sw, sh))
                StateCacheRecognizer._commit_known(self.shared["known_states"], self.shared["dirty_states"], chash, (sx, sy, sw, sh))
                self.shared["pending_post_save"] = None

            prev_frame[y:y + h, x:x + w] = roi
            return pack_vf_packet(cmds)

        return None


# ============================================================================
# 3. BINARY PACKERS FOR `b"VF"` PROTOCOL
# ============================================================================

def pack_save_state(state_hash: int, sx: int, sy: int, sw: int, sh: int) -> bytes:
    return struct.pack(">BQHHHH", CMD_SAVE_STATE, state_hash & 0xFFFFFFFFFFFFFFFF, sx, sy, sw, sh)


def pack_restore_state(state_hash: int, dx: int, dy: int, dw: int, dh: int) -> bytes:
    return struct.pack(">BQHHHH", CMD_RESTORE_STATE, state_hash & 0xFFFFFFFFFFFFFFFF, dx, dy, dw, dh)


def pack_transform_rect(sx: int, sy: int, sw: int, sh: int, dx: int, dy: int, dw: int, dh: int) -> bytes:
    return struct.pack(">BHHHHHHHH", CMD_TRANSFORM_RECT, sx, sy, sw, sh, dx, dy, dw, dh)


def pack_mark_dirty(state_hash: int, direction_flags: int = 1) -> bytes:
    return struct.pack(">BQB", CMD_MARK_DIRTY, state_hash & 0xFFFFFFFFFFFFFFFF, direction_flags & 0xFF)


def pack_jpeg_patch(dx: int, dy: int, dw: int, dh: int, jpeg_bytes: bytes) -> bytes:
    return struct.pack(">BHHHHI", CMD_JPEG_PATCH, dx, dy, dw, dh, len(jpeg_bytes)) + jpeg_bytes


def pack_overlay_meta(meta_dict: dict) -> bytes:
    raw = json.dumps(meta_dict).encode("utf-8")
    return struct.pack(">BI", CMD_OVERLAY_META, len(raw)) + raw


def pack_vf_packet(cmd_chunks: list[bytes]) -> bytes:
    return b"VF" + struct.pack(">B", min(255, len(cmd_chunks))) + b"".join(cmd_chunks[:255])


# ============================================================================
# 4. VIEWER-SIDE DECODER, RAM CACHE, OVERLAY & ANDROID-STYLE DEBUG VISUALIZER
# ============================================================================

class VisionViewerDecoder:
    """
    Decodes `b"VF"` packets on the Viewer, maintains the local RAM `state_cache`
    (for 0 KB Tab/Scroll/Selection restores), renders interactive Overlays,
    and provides an Android Developer Options style Surface Update Visualizer (`debug_mode`).
    """
    def __init__(self):
        self.state_cache = collections.OrderedDict()  # hash64 -> QImage
        self.dirty_hashes = set()
        self.virtual_y = 0
        self.bookmarked_y = 0
        self.viewport = None  # [x, y, w, h] in remote coords
        self.dirty_below = False
        self.saved_kb = 0.0
        self.last_event = ""
        self.last_event_ts = 0.0
        self.overlay_enabled = True

        # Android Developer Options style surface update visualization ("Show surface updates")
        self.debug_mode = True
        self.debug_rects = collections.deque(maxlen=40)  # dicts: x, y, w, h, kind, label, ts
        self.stats_cache_hits = 0
        self.stats_scroll_blits = 0
        self.stats_jpeg_patches = 0

        # Interactive overlay hit-boxes in canvas widget coordinates
        self.btn_bookmark_rect = None
        self.btn_jump_rect = None
        self.btn_debug_rect = None

    def clear(self):
        self.state_cache.clear()
        self.dirty_hashes.clear()
        self.debug_rects.clear()
        self.virtual_y = 0
        self.dirty_below = False

    def toggle_debug(self) -> bool:
        self.debug_mode = not self.debug_mode
        if not self.debug_mode:
            self.debug_rects.clear()
        self.last_event_ts = time.time()
        return self.debug_mode

    def _record_debug(self, x: int, y: int, w: int, h: int, kind: str, label: str):
        if not self.debug_mode or w <= 0 or h <= 0:
            return
        self.debug_rects.append({
            "x": x, "y": y, "w": w, "h": h,
            "kind": kind,
            "label": label,
            "ts": time.time(),
        })

    def on_full_frame(self, byte_len: int = 0, width: int = 1920, height: int = 1080):
        """Called when a full JPEG keyframe arrives."""
        self.stats_jpeg_patches += 1
        kb = f"{byte_len / 1024.0:.1f} KB" if byte_len else "Keyframe"
        self._record_debug(0, 0, width, height, "full", f"FULL FRAME ({kb})")

    def on_dl_patch(self, dx: int, dy: int, dw: int, dh: int, byte_len: int = 0):
        """Called when a standard `b"DL"` JPEG delta patch arrives."""
        self.stats_jpeg_patches += 1
        kb = f"{byte_len / 1024.0:.1f} KB" if byte_len else "JPEG"
        self._record_debug(dx, dy, dw, dh, "jpeg", f"NET JPEG ({kb})")

    def apply_vf_packet(self, data: bytes, base_qimage):
        """Convenience wrapper around apply_packet using PySide6 classes."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter
        self.apply_packet(data, base_qimage, QImage, QPainter, QRectF)

    def apply_packet(self, data: bytes, base_qimage, qimage_cls, qpainter_cls, qrectf_cls):
        """Executes all commands inside a `b"VF"` packet directly on `base_qimage`."""
        if base_qimage is None or base_qimage.isNull() or len(data) < 3:
            return
        num_cmds = data[2]
        offset = 3
        n = len(data)

        # Clear older debug rects from previous frames if a new batch of visual updates starts
        now = time.time()
        while self.debug_rects and (now - self.debug_rects[0]["ts"]) > 0.65:
            self.debug_rects.popleft()

        p = qpainter_cls(base_qimage)
        try:
            for _ in range(num_cmds):
                if offset >= n:
                    break
                cmd = data[offset]
                offset += 1

                if cmd == CMD_SAVE_STATE and offset + 16 <= n:
                    shash, sx, sy, sw, sh = struct.unpack_from(">QHHHH", data, offset)
                    offset += 16
                    sub_img = base_qimage.copy(sx, sy, sw, sh)
                    if not sub_img.isNull():
                        self.state_cache[shash] = sub_img
                        self.state_cache.move_to_end(shash)
                        self.dirty_hashes.discard(shash)
                        while len(self.state_cache) > 96:
                            ev_k, _ = self.state_cache.popitem(last=False)
                            self.dirty_hashes.discard(ev_k)

                elif cmd == CMD_RESTORE_STATE and offset + 16 <= n:
                    shash, dx, dy, dw, dh = struct.unpack_from(">QHHHH", data, offset)
                    offset += 16
                    cached_img = self.state_cache.get(shash)
                    if cached_img is not None and not cached_img.isNull():
                        p.drawImage(qrectf_cls(dx, dy, dw, dh), cached_img)
                        self.state_cache.move_to_end(shash)
                        self.stats_cache_hits += 1
                        self._record_debug(dx, dy, dw, dh, "cache", "RAM CACHE (0 KB)")
                    else:
                        self._record_debug(dx, dy, dw, dh, "miss", "CACHE MISS!")

                elif cmd == CMD_TRANSFORM_RECT and offset + 16 <= n:
                    sx, sy, sw, sh, dx, dy, dw, dh = struct.unpack_from(">HHHHHHHH", data, offset)
                    offset += 16
                    src_copy = base_qimage.copy(sx, sy, sw, sh)
                    if not src_copy.isNull():
                        p.drawImage(qrectf_cls(dx, dy, dw, dh), src_copy)
                        self.stats_scroll_blits += 1
                        shift_y = sy - dy
                        shift_x = sx - dx
                        lbl = f"GPU SCROLL (dy:{shift_y:+d}px, 0 KB)" if shift_y else f"GPU SCROLL (dx:{shift_x:+d}px, 0 KB)"
                        self._record_debug(dx, dy, dw, dh, "scroll", lbl)

                elif cmd == CMD_MARK_DIRTY and offset + 9 <= n:
                    shash, flags = struct.unpack_from(">QB", data, offset)
                    offset += 9
                    if shash:
                        self.dirty_hashes.add(shash)
                    if flags & 1:
                        self.dirty_below = True

                elif cmd == CMD_JPEG_PATCH and offset + 12 <= n:
                    dx, dy, dw, dh, jlen = struct.unpack_from(">HHHHI", data, offset)
                    offset += 12
                    if offset + jlen <= n:
                        patch = qimage_cls.fromData(data[offset:offset + jlen])
                        offset += jlen
                        if not patch.isNull():
                            p.drawImage(dx, dy, patch)
                            self.stats_jpeg_patches += 1
                            self._record_debug(dx, dy, dw, dh, "jpeg", f"NET JPEG ({jlen / 1024.0:.1f} KB)")

                elif cmd == CMD_OVERLAY_META and offset + 4 <= n:
                    mlen, = struct.unpack_from(">I", data, offset)
                    offset += 4
                    if offset + mlen <= n:
                        try:
                            meta = json.loads(data[offset:offset + mlen].decode("utf-8"))
                            self._update_meta(meta)
                        except Exception:
                            pass
                        offset += mlen
                else:
                    break
        finally:
            p.end()

    def _update_meta(self, meta: dict):
        self.last_event = meta.get("event", "")
        self.last_event_ts = time.time()
        if "saved_kb" in meta:
            self.saved_kb = meta["saved_kb"]
        if "virtual_y" in meta:
            self.virtual_y = meta["virtual_y"]
        if "viewport" in meta:
            self.viewport = meta["viewport"]
        if "dirty_below" in meta:
            self.dirty_below = bool(meta["dirty_below"])

    def paint_overlays(self, painter, target_rect, base_qimage=None):
        """
        Draws:
        1) Android Developer Options style color-tinted Surface Update overlays (when `debug_mode` is ON):
           - GREEN:  Restored from local Viewer RAM Cache (0 KB)
           - BLUE:   Shifted/copied locally on Viewer GPU during Scroll (0 KB)
           - RED:    Newly received Network JPEG patch (New strip / Residual / Scrollbar)
           - ORANGE: Full Keyframe
        2) Interactive Vision & Scroll Memory Pill at top-right (`📌` Bookmark, `⏪` Scroll-Back, `🔬` Debug Toggle).
        """
        from PySide6.QtCore import Qt, QRectF
        from PySide6.QtGui import QColor, QPen

        self.btn_bookmark_rect = None
        self.btn_jump_rect = None
        self.btn_debug_rect = None

        img_x = target_rect.x()
        img_y = target_rect.y()
        img_w = target_rect.width()
        img_h = target_rect.height()

        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)

        # --------------------------------------------------------------------
        # 1. ANDROID DEVELOPER MODE SURFACE UPDATE TINTING (`self.debug_mode`)
        # --------------------------------------------------------------------
        if self.debug_mode:
            rem_w = float(base_qimage.width()) if (base_qimage is not None and not base_qimage.isNull()) else 1920.0
            rem_h = float(base_qimage.height()) if (base_qimage is not None and not base_qimage.isNull()) else 1080.0
            sx_scale = img_w / max(1.0, rem_w)
            sy_scale = img_h / max(1.0, rem_h)

            now = time.time()
            # Show rects from the last 1.2s (or keep the most recent 6 rects visible for inspection!)
            recent = [r for r in self.debug_rects if (now - r["ts"]) <= 1.2]
            if not recent and self.debug_rects:
                recent = list(self.debug_rects)[-6:]

            palette = {
                "cache":  (QColor(16, 185, 129, 75),  QColor(16, 185, 129, 235),  QColor(6, 95, 70, 225)),     # GREEN: RAM Cache
                "scroll": (QColor(14, 165, 233, 60),  QColor(56, 189, 248, 230),  QColor(12, 74, 110, 225)),   # BLUE:  GPU Scroll Copy
                "jpeg":   (QColor(244, 63, 94, 78),   QColor(251, 113, 133, 240), QColor(136, 19, 55, 225)),   # RED:   Network JPEG
                "full":   (QColor(245, 158, 11, 40),  QColor(251, 191, 36, 220),  QColor(120, 53, 15, 225)),   # AMBER: Full Keyframe
                "miss":   (QColor(220, 38, 38, 130),  QColor(254, 202, 202, 255), QColor(127, 29, 29, 240)),   # BRIGHT RED: Cache Miss
            }

            for item in recent:
                rx = img_x + item["x"] * sx_scale
                ry = img_y + item["y"] * sy_scale
                rw = item["w"] * sx_scale
                rh = item["h"] * sy_scale
                rect_f = QRectF(rx, ry, rw, rh)

                fill_c, border_c, tag_bg = palette.get(item["kind"], palette["jpeg"])
                pen = QPen(border_c, 2.0)
                painter.setPen(pen)
                painter.setBrush(fill_c)
                painter.drawRect(rect_f)

                # Draw readable badge inside the region if large enough
                if rw >= 70.0 and rh >= 20.0:
                    lbl = item["label"]
                    tw = min(rw - 4.0, max(95.0, len(lbl) * 6.8 + 12.0))
                    th = 18.0
                    tag_rect = QRectF(rx + 3.0, ry + 3.0, tw, th)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(tag_bg)
                    painter.drawRoundedRect(tag_rect, 4.0, 4.0)
                    painter.setPen(QColor(255, 255, 255))
                    painter.drawText(
                        tag_rect.adjusted(5.0, 0.0, -3.0, 0.0),
                        int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                        lbl
                    )

            # Top-left Legend Bar (Android Developer Mode style)
            leg_w = min(img_w - 28.0, 620.0)
            leg_h = 28.0
            leg_rect = QRectF(img_x + 14.0, img_y + 14.0, leg_w, leg_h)
            painter.setPen(QColor(56, 189, 248, 150))
            painter.setBrush(QColor(15, 23, 42, 225))
            painter.drawRoundedRect(leg_rect, 7.0, 7.0)

            legend_txt = (
                f"🔬 DEBUG  |  🟢 Cache ({self.stats_cache_hits})   "
                f"🔵 Scroll-Copy ({self.stats_scroll_blits})   "
                f"🔴 Net-JPEG ({self.stats_jpeg_patches})   "
                f"📦 RAM: {len(self.state_cache)} states"
            )
            painter.setPen(QColor(241, 245, 249))
            painter.drawText(
                leg_rect.adjusted(10.0, 0.0, -10.0, 0.0),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                legend_txt
            )

        # --------------------------------------------------------------------
        # 2. TOP-RIGHT VISION MEMORY & AUTO-SCROLL-BACK PILL
        # --------------------------------------------------------------------
        if not self.overlay_enabled:
            painter.restore()
            return

        age = time.time() - self.last_event_ts
        delta_from_mark = self.virtual_y - self.bookmarked_y
        if not self.debug_mode and age > 6.0 and abs(delta_from_mark) < 40 and not self.dirty_below:
            painter.restore()
            return

        pill_w = 314.0
        pill_h = 30.0
        px = img_x + img_w - pill_w - 14.0
        py = img_y + 14.0

        bg_rect = QRectF(px, py, pill_w, pill_h)
        painter.setPen(QColor(56, 189, 248, 120))
        painter.setBrush(QColor(15, 23, 42, 220))
        painter.drawRoundedRect(bg_rect, 8.0, 8.0)

        dirty_tag = " ⚠️Dirty" if self.dirty_below else ""
        info_txt = f"⚡ -{self.saved_kb:.0f} KB | Y:{self.virtual_y:+d}px{dirty_tag}"
        painter.setPen(QColor(226, 232, 240))
        text_rect = QRectF(px + 10.0, py, 165.0, pill_h)
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), info_txt)

        # Button 1: [📌] Bookmark current scroll position
        b1 = QRectF(px + 178.0, py + 4.0, 38.0, 22.0)
        painter.setBrush(QColor(37, 99, 235, 220))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(b1, 5.0, 5.0)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(b1, int(Qt.AlignmentFlag.AlignCenter), "📌")
        self.btn_bookmark_rect = b1

        # Button 2: [⏪] Auto-scroll back to bookmarked position
        b2 = QRectF(px + 221.0, py + 4.0, 40.0, 22.0)
        active_jump = abs(delta_from_mark) >= 20
        painter.setBrush(QColor(14, 165, 233, 230) if active_jump else QColor(51, 65, 85, 180))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(b2, 5.0, 5.0)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(b2, int(Qt.AlignmentFlag.AlignCenter), "⏪")
        self.btn_jump_rect = b2

        # Button 3: [🔬] Toggle Android-style Surface Update Debug coloring
        b3 = QRectF(px + 266.0, py + 4.0, 40.0, 22.0)
        painter.setBrush(QColor(16, 185, 129, 235) if self.debug_mode else QColor(51, 65, 85, 180))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(b3, 5.0, 5.0)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(b3, int(Qt.AlignmentFlag.AlignCenter), "🔬")
        self.btn_debug_rect = b3

        painter.restore()

    def handle_overlay_click(self, pos, send_input_fn, update_fn=None) -> bool:
        """
        Checks if a mouse click on the Viewer canvas hit one of the Vision Overlay buttons.
        Returns True if the click was consumed by the overlay.
        """
        pos_x = pos.x() if hasattr(pos, "x") else float(pos[0])
        pos_y = pos.y() if hasattr(pos, "y") else float(pos[1])

        if self.btn_debug_rect and self.btn_debug_rect.contains(pos_x, pos_y):
            self.toggle_debug()
            if update_fn:
                update_fn()
            return True

        if self.btn_bookmark_rect and self.btn_bookmark_rect.contains(pos_x, pos_y):
            self.bookmarked_y = self.virtual_y
            self.dirty_below = False
            self.last_event_ts = time.time()
            if update_fn:
                update_fn()
            return True

        if self.btn_jump_rect and self.btn_jump_rect.contains(pos_x, pos_y):
            diff_y = self.virtual_y - self.bookmarked_y
            if abs(diff_y) >= 20:
                ticks = max(1, min(25, round(abs(diff_y) / 90.0)))
                delta_dir = 1 if diff_y > 0 else -1
                for _ in range(ticks):
                    send_input_fn({"event": "mouse_wheel", "delta": delta_dir})
                self.virtual_y = self.bookmarked_y
                self.last_event_ts = time.time()
            if update_fn:
                update_fn()
            return True

        return False
