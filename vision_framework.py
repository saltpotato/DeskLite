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
import logging
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
CMD_UPDATE_BG = 0x07
CMD_RESTORE_BG = 0x08


try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


def fast_region_hash(arr: np.ndarray) -> int:
    """Computes an ultra-fast 64-bit content fingerprint of a 3D BGR/BGRA numpy region.
    Uses 6-bit color quantization (>> 2) so 1-2 LSB DWM/GPU dithering noise never breaks cache hits."""
    h, w = arr.shape[:2]
    if h < 4 or w < 4:
        return 0
    step = 2 if (w * h < 4096) else 4
    sub = np.ascontiguousarray(arr[::step, ::step, :3] >> 2)
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
    - Canonical & Sub-Region Peeling: When moving the mouse from Button A to adjacent Button B
      (or when Hover-Out has a slightly different diff box than Hover-In), restores Button A
      from RAM cache (0 KB) and shrinks the remaining patch to only Button B!
    """
    name = "StateCacheRecognizer"

    def analyze(self, ctx: FrameContext):
        x, y, w, h = ctx.dirty_rect
        if w < 12 or h < 12:
            return None

        prev_roi = ctx.prev_frame[y:y + h, x:x + w]
        curr_roi = ctx.curr_frame[y:y + h, x:x + w]

        prev_hash = fast_region_hash(prev_roi)
        curr_hash = fast_region_hash(curr_roi)
        if prev_hash == 0 or curr_hash == 0:
            return None

        known: collections.OrderedDict = ctx.shared["known_states"]
        dirty_set: set = ctx.shared["dirty_states"]
        cmds = []

        # 1. Exact dirty_rect cache hit check
        if prev_hash != curr_hash and curr_hash in known and curr_hash not in dirty_set:
            if prev_hash not in known:
                cmds.append(pack_save_state(prev_hash, x, y, w, h))
                self._commit_known(known, dirty_set, prev_hash, (x, y, w, h))

            known.move_to_end(curr_hash)
            cmds.append(pack_restore_state(curr_hash, x, y, w, h))
            est_saved = max(1024, int(w * h * 0.12))
            ctx.shared["bytes_saved"] += est_saved
            cmds.append(pack_overlay_meta({
                "event": "cache_hit",
                "rect": [x, y, w, h],
                "saved_kb": round(ctx.shared["bytes_saved"] / 1024.0, 1)
            }))
            if ctx.shared.get("debug_mode", True):
                logging.info(
                    f"[VF-DEBUG][HOST] CACHE_HIT hash={curr_hash & 0xFFFFFFFF:08x} "
                    f"rect=({x},{y},{w}x{h}) saved={est_saved / 1024.0:.1f}KB "
                    f"total_saved={ctx.shared['bytes_saved'] / 1024.0:.1f}KB"
                )
            ctx.prev_frame[y:y + h, x:x + w] = curr_roi
            return cmds

        # 2. Canonical & Sub-Region Known-Rect Peeling:
        # Check if any recently cached rectangle R_k = (kx, ky, kw, kh) overlapping dirty_rect
        # has transitioned to a known cached state (e.g. Button A un-hovering while adjacent Button B hovers,
        # or Hover-Out bounding box differing by a few pixels from Hover-In).
        seen_rects = set()
        peeled_any = False
        for k_rect in reversed(list(known.values())):
            if k_rect == (x, y, w, h) or k_rect in seen_rects:
                continue
            seen_rects.add(k_rect)
            if len(seen_rects) > 28:
                break
            kx, ky, kw, kh = k_rect
            if kw * kh > w * h * 4 or kw * kh < w * h * 0.25:
                continue
            ix1, iy1 = max(x, kx), max(y, ky)
            ix2, iy2 = min(x + w, kx + kw), min(y + h, ky + kh)
            iw, ih = ix2 - ix1, iy2 - iy1
            if iw < 12 or ih < 12:
                continue
            inter_area = iw * ih
            if inter_area < 0.45 * (kw * kh) and inter_area < 0.45 * (w * h):
                continue

            k_curr_roi = ctx.curr_frame[ky:ky + kh, kx:kx + kw]
            k_curr_hash = fast_region_hash(k_curr_roi)
            if k_curr_hash not in known or k_curr_hash in dirty_set or known[k_curr_hash][2:] != (kw, kh):
                continue

            k_prev_roi = ctx.prev_frame[ky:ky + kh, kx:kx + kw]
            k_diff = np.abs(k_prev_roi[::2, ::2, :3].astype(np.int16) - k_curr_roi[::2, ::2, :3].astype(np.int16)).max(axis=2) > 3
            if not np.any(k_diff):
                continue

            # Save the outgoing state at k_rect (e.g. completed hover state) before restoring
            k_prev_hash = fast_region_hash(k_prev_roi)
            if k_prev_hash and k_prev_hash != k_curr_hash and k_prev_hash not in known:
                cmds.append(pack_save_state(k_prev_hash, kx, ky, kw, kh))
                self._commit_known(known, dirty_set, k_prev_hash, k_rect)

            known.move_to_end(k_curr_hash)
            cmds.append(pack_restore_state(k_curr_hash, kx, ky, kw, kh))
            est_saved = max(1024, int(kw * kh * 0.12))
            ctx.shared["bytes_saved"] += est_saved
            cmds.append(pack_overlay_meta({
                "event": "cache_hit",
                "rect": [kx, ky, kw, kh],
                "saved_kb": round(ctx.shared["bytes_saved"] / 1024.0, 1)
            }))
            if ctx.shared.get("debug_mode", True):
                logging.info(
                    f"[VF-DEBUG][HOST] CACHE_HIT_SUBRECT hash={k_curr_hash & 0xFFFFFFFF:08x} "
                    f"rect=({kx},{ky},{kw}x{kh}) inside=({x},{y},{w}x{h}) saved={est_saved / 1024.0:.1f}KB"
                )
            ctx.prev_frame[ky:ky + kh, kx:kx + kw] = k_curr_roi
            peeled_any = True

        if peeled_any:
            # Check if anything inside dirty_rect is STILL different after peeling the cached sub-rect(s)
            rem_prev = ctx.prev_frame[y:y + h:2, x:x + w:2, :3]
            rem_curr = ctx.curr_frame[y:y + h:2, x:x + w:2, :3]
            rem_mask = np.abs(rem_prev.astype(np.int16) - rem_curr.astype(np.int16)).max(axis=2) > 3
            rys, rxs = np.nonzero(rem_mask)
            if rys.size == 0:
                return cmds

            # Shrink remaining dirty area to only the uncached part (e.g. newly hovered Button B)
            fh, fw = ctx.curr_frame.shape[:2]
            rx1 = max(0, x + int(rxs.min()) * 2)
            ry1 = max(0, y + int(rys.min()) * 2)
            rx2 = min(fw, x + (int(rxs.max()) + 1) * 2)
            ry2 = min(fh, y + (int(rys.max()) + 1) * 2)
            rw, rh = max(12, rx2 - rx1), max(12, ry2 - ry1)
            rx2, ry2 = min(fw, rx1 + rw), min(fh, ry1 + rh)
            rw, rh = int(rx2 - rx1), int(ry2 - ry1)

            rem_prev_roi = ctx.prev_frame[ry1:ry2, rx1:rx2]
            rem_curr_roi = ctx.curr_frame[ry1:ry2, rx1:rx2]
            r_prev_hash = fast_region_hash(rem_prev_roi)
            r_curr_hash = fast_region_hash(rem_curr_roi)

            if r_prev_hash and r_prev_hash not in known:
                cmds.append(pack_save_state(r_prev_hash, rx1, ry1, rw, rh))
                self._commit_known(known, dirty_set, r_prev_hash, (rx1, ry1, rw, rh))

            jpeg_bytes = ctx.encode_jpeg(rem_curr_roi, ctx.quality)
            cmds.append(pack_jpeg_patch(rx1, ry1, rw, rh, jpeg_bytes))

            if r_curr_hash and r_curr_hash not in known:
                cmds.append(pack_save_state(r_curr_hash, rx1, ry1, rw, rh))
                self._commit_known(known, dirty_set, r_curr_hash, (rx1, ry1, rw, rh))

            if ctx.shared.get("debug_mode", True):
                logging.info(
                    f"[VF-DEBUG][HOST] PEELED_JPEG_PATCH rect=({rx1},{ry1},{rw}x{rh}) "
                    f"size={len(jpeg_bytes) / 1024.0:.1f}KB"
                )
            ctx.prev_frame[ry1:ry2, rx1:rx2] = rem_curr_roi
            return cmds

        if prev_hash != curr_hash:
            if prev_hash not in known:
                ctx.shared["pending_pre_save"] = (prev_hash, x, y, w, h)
            ctx.shared["pending_post_save"] = (curr_hash, x, y, w, h)
        return None

    @staticmethod
    def _commit_known(known: collections.OrderedDict, dirty_set: set, shash: int, rect: tuple):
        known[shash] = rect
        known.move_to_end(shash)
        dirty_set.discard(shash)
        while len(known) > 240:
            evicted, _ = known.popitem(last=False)
            dirty_set.discard(evicted)


class Scroll2DAndSliderRecognizer(BaseRecognizer):
    """
    Detects 2D scrolling (vertical dy and horizontal dx) + monitors vertical/horizontal
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

        # 1. Detect vertical shift candidates (dy) using normalized 1D gradient correlation
        dy_candidates = self._detect_1d_shift_candidates(
            prev_roi[:inner_h, :inner_w, 1],
            curr_roi[:inner_h, :inner_w, 1],
            axis=1,
            max_shift=min(h // 2, 360)
        )
        dy = 0
        best_score = 0.0
        for cand_dy in dy_candidates:
            ok, score = self._verify_overlap(prev_roi, curr_roi, 0, cand_dy, inner_w, inner_h)
            if ok and score > best_score:
                dy = cand_dy
                best_score = score

        # 2. Detect horizontal shift candidates (dx) if vertical shift is 0
        dx = 0
        dx_candidates = []
        if dy == 0:
            dx_candidates = self._detect_1d_shift_candidates(
                prev_roi[:inner_h, :inner_w, 1],
                curr_roi[:inner_h, :inner_w, 1],
                axis=0,
                max_shift=min(w // 2, 360)
            )
            for cand_dx in dx_candidates:
                ok, score = self._verify_overlap(prev_roi, curr_roi, cand_dx, 0, inner_w, inner_h)
                if ok and score > best_score:
                    dx = cand_dx
                    best_score = score

        # 3. Check vertical scrollbar strip (right edge) for offscreen content growth (DIRTY check)
        slider_changed = self._check_scrollbar_dirty(prev_roi[:, inner_w:], curr_roi[:, inner_w:])
        if dy == 0 and dx == 0:
            if slider_changed:
                ctx.shared["dirty_below"] = True
                ctx.shared["pending_cmds"].append(pack_mark_dirty(0, 1))
                ctx.shared["pending_cmds"].append(pack_overlay_meta({
                    "event": "dirty_below",
                    "viewport": [int(x), int(y), int(w), int(h)],
                    "virtual_y": int(ctx.shared.get("virtual_y", 0)),
                    "dirty_below": True,
                    "saved_kb": round(ctx.shared["bytes_saved"] / 1024.0, 1)
                }))
                if ctx.shared.get("debug_mode", True):
                    logging.info(
                        f"[VF-DEBUG][HOST] DIRTY_SLIDER rect=({x},{y},{w}x{h}) "
                        f"vY={ctx.shared.get('virtual_y', 0):+d}px"
                    )
            elif (dy_candidates or dx_candidates) and ctx.shared.get("debug_mode", True):
                c_dy = dy_candidates[0] if dy_candidates else 0
                c_dx = dx_candidates[0] if dx_candidates else 0
                logging.info(
                    f"[VF-DEBUG][HOST] SCROLL_REJECT_OVERLAP rect=({x},{y},{w}x{h}) "
                    f"cand_dx={c_dx:+d} cand_dy={c_dy:+d} (match_ratio < 0.68)"
                )
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
            shifted_overlap = ctx.prev_frame[src_y:src_y + overlap_h, x:x + inner_w]
            target_overlap = ctx.curr_frame[dst_y:dst_y + overlap_h, x:x + inner_w]
            residual_rects = self._find_residual_rects(shifted_overlap, target_overlap, x, dst_y)
            if residual_rects is None:
                if ctx.shared.get("debug_mode", True):
                    logging.info(
                        f"[VF-DEBUG][HOST] SCROLL_REJECT_RESIDUAL rect=({x},{y},{w}x{h}) "
                        f"dy={dy:+d} (overlap mismatch > threshold)"
                    )
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
            ctx.shared["viewport"] = [int(x), int(y), int(w), int(h)]

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

            # 3) Encode newly revealed strip (or restore from Viewer RAM cache if seen before)
            new_strip = ctx.curr_frame[new_y:new_y + sh_abs, x:x + w]
            strip_cmds, strip_from_cache = self._encode_or_restore_strip(ctx, new_strip, x, new_y, w, sh_abs)
            cmds.extend(strip_cmds)

            # 4) Encode narrow right scrollbar track
            sb_strip = ctx.curr_frame[dst_y:dst_y + overlap_h, x + inner_w:x + w]
            cmds.append(pack_jpeg_patch(x + inner_w, dst_y, sb_w, overlap_h, ctx.encode_jpeg(sb_strip, ctx.quality)))

            # 5) Patch any stationary/sticky UI sub-rectangles inside the overlap so nothing ever smears!
            for rx, ry, rw, rh in residual_rects:
                res_patch = ctx.curr_frame[ry:ry + rh, rx:rx + rw]
                cmds.append(pack_jpeg_patch(rx, ry, rw, rh, ctx.encode_jpeg(res_patch, ctx.quality)))

            # Sync prev_frame to curr_frame inside the scroll viewport so sub-threshold AA differences
            # never accumulate across consecutive scroll frames or pollute subsequent mouse hovers!
            ctx.prev_frame[y:y + h, x:x + w] = ctx.curr_frame[y:y + h, x:x + w]

            # 6) Save the complete post-scroll viewport state in Viewer RAM cache
            post_hash = fast_region_hash(ctx.curr_frame[y:y + h, x:x + w])
            if post_hash and post_hash not in ctx.shared["known_states"]:
                cmds.append(pack_save_state(post_hash, x, y, w, h))
                StateCacheRecognizer._commit_known(ctx.shared["known_states"], ctx.shared["dirty_states"], post_hash, (x, y, w, h))

            est_saved = max(512, int(inner_w * overlap_h * 0.10))
            ctx.shared["bytes_saved"] += est_saved
            cmds.append(pack_overlay_meta({
                "event": "scroll",
                "dx": int(dx),
                "dy": int(dy),
                "viewport": [int(x), int(y), int(w), int(h)],
                "virtual_y": int(ctx.shared["virtual_y"]),
                "dirty_below": ctx.shared.get("dirty_below", False),
                "saved_kb": round(ctx.shared["bytes_saved"] / 1024.0, 1)
            }))
            if ctx.shared.get("debug_mode", True):
                logging.info(
                    f"[VF-DEBUG][HOST] SCROLL_Y rect=({x},{y},{w}x{h}) dy={dy:+d}px "
                    f"vY={ctx.shared['virtual_y']:+d}px strip={'CACHE_HIT' if strip_from_cache else 'NET_JPEG'} "
                    f"residuals={len(residual_rects)} {residual_rects} "
                    f"saved_total={ctx.shared['bytes_saved'] / 1024.0:.1f}KB"
                )
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

            shifted_overlap = ctx.prev_frame[y:y + h, src_x:src_x + overlap_w]
            target_overlap = ctx.curr_frame[y:y + h, dst_x:dst_x + overlap_w]
            residual_rects = self._find_residual_rects(shifted_overlap, target_overlap, dst_x, y)
            if residual_rects is None:
                if ctx.shared.get("debug_mode", True):
                    logging.info(
                        f"[VF-DEBUG][HOST] SCROLL_REJECT_RESIDUAL rect=({x},{y},{w}x{h}) dx={dx:+d}"
                    )
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

            new_strip = ctx.curr_frame[y:y + h, new_x:new_x + sw_abs]
            strip_cmds, strip_from_cache = self._encode_or_restore_strip(ctx, new_strip, new_x, y, sw_abs, h)
            cmds.extend(strip_cmds)

            for rx, ry, rw, rh in residual_rects:
                res_patch = ctx.curr_frame[ry:ry + rh, rx:rx + rw]
                cmds.append(pack_jpeg_patch(rx, ry, rw, rh, ctx.encode_jpeg(res_patch, ctx.quality)))

            ctx.prev_frame[y:y + h, x:x + w] = ctx.curr_frame[y:y + h, x:x + w]

            est_saved = max(512, int(overlap_w * h * 0.10))
            ctx.shared["bytes_saved"] += est_saved
            cmds.append(pack_overlay_meta({
                "event": "scroll_x",
                "dx": int(dx),
                "dy": 0,
                "viewport": [int(x), int(y), int(w), int(h)],
                "virtual_y": int(ctx.shared.get("virtual_y", 0)),
                "saved_kb": round(ctx.shared["bytes_saved"] / 1024.0, 1)
            }))
            if ctx.shared.get("debug_mode", True):
                logging.info(
                    f"[VF-DEBUG][HOST] SCROLL_X rect=({x},{y},{w}x{h}) dx={dx:+d}px "
                    f"strip={'CACHE_HIT' if strip_from_cache else 'NET_JPEG'} "
                    f"residuals={len(residual_rects)} {residual_rects}"
                )
            return cmds

        return None

    def _encode_or_restore_strip(self, ctx: FrameContext, strip_arr: np.ndarray, sx: int, sy: int, sw: int, sh: int):
        shash = fast_region_hash(strip_arr)
        known = ctx.shared["known_states"]
        dirty = ctx.shared["dirty_states"]
        if shash and shash in known and shash not in dirty:
            ctx.shared["bytes_saved"] += max(512, int(sw * sh * 0.12))
            known.move_to_end(shash)
            return [pack_restore_state(shash, sx, sy, sw, sh)], True

        out = [pack_jpeg_patch(sx, sy, sw, sh, ctx.encode_jpeg(strip_arr, ctx.quality))]
        if shash:
            out.append(pack_save_state(shash, sx, sy, sw, sh))
            StateCacheRecognizer._commit_known(known, dirty, shash, (sx, sy, sw, sh))
        return out, False

    @staticmethod
    def _find_residual_rects(shifted: np.ndarray, target: np.ndarray, base_x: int, base_y: int):
        """
        Compares the shifted overlap region against the true target region on a 4x4 grid.
        Peels off stationary top/bottom headers and left/right borders into thin strips
        plus any clustered interior stationary elements (ignoring isolated 1-cell ClearType shimmer).
        Returns None if the mismatch area exceeds 32% of the overlap (abort scroll transform).
        """
        oh, ow = shifted.shape[:2]
        if oh < 8 or ow < 8:
            return []
        diff = np.abs(shifted[::4, ::4, :3].astype(np.int16) - target[::4, ::4, :3].astype(np.int16)).max(axis=2)
        bad = diff > 16
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
            rects.append((int(base_x), int(base_y), int(ow), int(py2)))

        # 2. Peel stationary bottom rows (e.g. bottom border or sticky footer)
        orig_r1 = r1
        while r1 > r0 and float(bad[r1 - 1, c0:c1].mean()) > 0.20:
            r1 -= 1
        if r1 < orig_r1:
            py1 = max(0, r1 * 4 - 4)
            rects.append((int(base_x), int(base_y + py1), int(ow), int(oh - py1)))

        # 3. Peel stationary left columns (e.g. left border or sidebar)
        mid_y1 = min(oh, r0 * 4)
        mid_y2 = max(mid_y1, min(oh, r1 * 4))
        mid_h = mid_y2 - mid_y1
        if mid_h > 0 and r0 < r1:
            while c0 < c1 and float(bad[r0:r1, c0].mean()) > 0.20:
                c0 += 1
            if c0 > 0:
                px2 = min(ow, c0 * 4 + 4)
                rects.append((int(base_x), int(base_y + mid_y1), int(px2), int(mid_h)))

            # 4. Peel stationary right columns
            orig_c1 = c1
            while c1 > c0 and float(bad[r0:r1, c1 - 1].mean()) > 0.20:
                c1 -= 1
            if c1 < orig_c1:
                px1 = max(0, c1 * 4 - 4)
                rects.append((int(base_x + px1), int(base_y + mid_y1), int(ow - px1), int(mid_h)))

        # 5. Any remaining interior stationary elements (filter out isolated 1-cell ClearType AA noise)
        if r0 < r1 and c0 < c1:
            sub = bad[r0:r1, c0:c1]
            if np.any(sub):
                if HAS_CV2 and sub.shape[0] >= 3 and sub.shape[1] >= 3:
                    sub_u8 = sub.astype(np.uint8)
                    # Filter out single isolated 4x4 cells that have no neighbors in a 3x3 window
                    nbr_count = cv2.boxFilter(sub_u8, ddepth=-1, ksize=(3, 3), normalize=False)
                    clustered = (sub_u8 > 0) & (nbr_count >= 2)
                    if np.any(clustered):
                        dil = cv2.dilate(clustered.astype(np.uint8), np.ones((5, 5), dtype=np.uint8))
                        num_l, _, stats, _ = cv2.connectedComponentsWithStats(dil, connectivity=8)
                        for lid in range(1, min(num_l, 5)):
                            gx, gy, gw_c, gh_c = int(stats[lid, 0]), int(stats[lid, 1]), int(stats[lid, 2]), int(stats[lid, 3])
                            iy1 = max(0, (r0 + gy) * 4)
                            iy2 = min(oh, (r0 + gy + gh_c) * 4)
                            ix1 = max(0, (c0 + gx) * 4)
                            ix2 = min(ow, (c0 + gx + gw_c) * 4)
                            if ix2 > ix1 and iy2 > iy1:
                                rects.append((int(base_x + ix1), int(base_y + iy1), int(ix2 - ix1), int(iy2 - iy1)))
                else:
                    ys, xs = np.nonzero(sub)
                    if ys.size >= 2:
                        iy1 = max(0, (r0 + int(ys.min())) * 4 - 4)
                        iy2 = min(oh, (r0 + int(ys.max()) + 1) * 4 + 4)
                        ix1 = max(0, (c0 + int(xs.min())) * 4 - 4)
                        ix2 = min(ow, (c0 + int(xs.max()) + 1) * 4 + 4)
                        if ix2 > ix1 and iy2 > iy1:
                            rects.append((int(base_x + ix1), int(base_y + iy1), int(ix2 - ix1), int(iy2 - iy1)))

        total_area = sum(rw * rh for _, _, rw, rh in rects)
        if total_area > (ow * oh * 0.42):
            return None
        return rects

    @staticmethod
    def _detect_1d_shift_candidates(prev_g: np.ndarray, curr_g: np.ndarray, axis: int, max_shift: int) -> list[int]:
        """
        Finds up to 3 ranked candidate pixel shifts along `axis` (1=vertical dy, 0=horizontal dx)
        using normalized 1D gradient Pearson correlation across all lags. Runs in <0.4ms in numpy.
        """
        p1 = prev_g.mean(axis=axis, dtype=np.float32)
        p2 = curr_g.mean(axis=axis, dtype=np.float32)
        d1 = np.diff(p1)
        d2 = np.diff(p2)
        n = len(d1)
        if n < 64 or float(np.std(d1)) < 0.6 or float(np.std(d2)) < 0.6:
            return []

        margin = min(max_shift, n // 3)
        if margin < 8:
            return []
        tpl = d2[margin:n - margin]
        tpl_len = len(tpl)
        if tpl_len < 16:
            return []
        tpl_norm = tpl - tpl.mean()
        tpl_energy = float(np.dot(tpl_norm, tpl_norm))
        if tpl_energy < 1e-3:
            return []

        d1_zm = d1 - d1.mean()
        corr = np.correlate(d1_zm, tpl_norm, mode="valid")
        cs = np.cumsum(np.insert(d1_zm * d1_zm, 0, 0.0))
        seg_energies = np.maximum(1e-5, cs[tpl_len:] - cs[:-tpl_len])
        norm_corr = corr / np.sqrt(seg_energies * tpl_energy)

        # Zero out tiny shifts (< 4px)
        center = margin
        lo = max(0, center - 3)
        hi = min(len(norm_corr), center + 4)
        norm_corr_work = norm_corr.copy()
        norm_corr_work[lo:hi] = -1.0

        candidates = []
        for _ in range(3):
            best_idx = int(np.argmax(norm_corr_work))
            best_val = float(norm_corr_work[best_idx])
            if best_val < 0.65:
                break
            shift = best_idx - margin
            if abs(shift) >= 4:
                candidates.append(int(shift))
            # Suppress neighborhood around this peak to find distinct secondary peaks
            s_lo = max(0, best_idx - 5)
            s_hi = min(len(norm_corr_work), best_idx + 6)
            norm_corr_work[s_lo:s_hi] = -1.0

        return candidates

    @staticmethod
    def _verify_overlap(prev_roi: np.ndarray, curr_roi: np.ndarray, dx: int, dy: int, w: int, h: int) -> tuple[bool, float]:
        """Verifies that the shifted overlapping region genuinely matches on a coarse 8x8 pixel grid.
        Uses outlier-robust inlier ratio (percentage of pixels within <= 12 RGB units) so sticky
        headers or sidebars never skew the score."""
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
            return False, 0.0
        diff_max = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
        inlier_ratio = float((diff_max <= 12).mean())
        return inlier_ratio >= 0.68, inlier_ratio

    @staticmethod
    def _check_scrollbar_dirty(prev_sb: np.ndarray, curr_sb: np.ndarray) -> bool:
        """Checks if the vertical scrollbar track changed significantly while content stayed static."""
        if prev_sb.size == 0 or prev_sb.shape != curr_sb.shape:
            return False
        diff = np.abs(prev_sb[:, :, :3].astype(np.int16) - curr_sb[:, :, :3].astype(np.int16))
        changed_rows = (diff.max(axis=(1, 2)) > 18).sum()
        return int(changed_rows) >= 6


class WindowMove2DRecognizer(BaseRecognizer):
    """
    Detects 2D diagonal/horizontal/vertical window dragging, floating dialog movement,
    and 2D canvas/map panning without any neural network model (~0.25ms in numpy/OpenCV):
    1. Coarse 2D translation search on 4x-downsampled 2D edge maps (`cv2.matchTemplate`).
    2. Shifts the moved window locally on the Viewer's GPU (`CMD_TRANSFORM_RECT`, 0 KB).
    3. Maintains a synchronized 2D Background Surface (`bg_surface`, 0 KB) so any desktop,
       wallpaper, or underlying window uncovered behind a dragged window is restored instantly
       without sending network JPEGs (`CMD_RESTORE_BG`, 0 KB)!
    """
    name = "WindowMove2DRecognizer"

    def analyze(self, ctx: FrameContext):
        if not HAS_CV2:
            return None
        x, y, w, h = ctx.dirty_rect
        if w < 120 or h < 90:
            return None

        prev_roi = ctx.prev_frame[y:y + h, x:x + w]
        curr_roi = ctx.curr_frame[y:y + h, x:x + w]

        mv = self._detect_2d_translation(prev_roi, curr_roi)
        if mv is None:
            return None
        mv_x, mv_y = mv
        # Window moves are 2D diagonal mouse drags across the desktop.
        # Pure 1D vertical (mv_x == 0) or horizontal (mv_y == 0) shifts are in-container scrolls,
        # which belong exclusively to Scroll2DAndSliderRecognizer.
        if abs(mv_x) < 2 or abs(mv_y) < 2:
            return None

        src_x, src_y, dst_x, dst_y, win_w, win_h = self._get_window_coords(x, y, w, h, mv_x, mv_y)
        if win_w < 64 or win_h < 48:
            return None

        shifted = prev_roi[src_y - y : src_y - y + win_h, src_x - x : src_x - x + win_w]
        target = curr_roi[dst_y - y : dst_y - y + win_h, dst_x - x : dst_x - x + win_w]

        diff_8 = np.abs(shifted[::8, ::8, :3].astype(np.int16) - target[::8, ::8, :3].astype(np.int16)).max(axis=2)
        inlier_ratio = float((diff_8 <= 14).mean())
        if inlier_ratio < 0.65:
            return None

        residual_rects = Scroll2DAndSliderRecognizer._find_residual_rects(
            shifted, target, dst_x, dst_y
        )
        if residual_rects is None:
            return None

        cmds = list(ctx.shared["pending_cmds"])
        ctx.shared["pending_cmds"].clear()
        ctx.shared["pending_pre_save"] = None
        ctx.shared["pending_post_save"] = None

        bg_frame = ctx.shared.setdefault("bg_frame", None)
        if bg_frame is None or bg_frame.shape != ctx.curr_frame.shape:
            ctx.shared["bg_frame"] = ctx.prev_frame.copy()
            bg_frame = ctx.shared["bg_frame"]

        # 1) Save newly covered background strips into Viewer's Background Surface BEFORE window moves
        abs_mx = abs(mv_x)
        sub_w = win_w - abs_mx

        # Covered X strip
        if mv_x > 0:
            cov_x, cov_w = src_x + win_w, mv_x
            bg_frame[dst_y : dst_y + win_h, cov_x : cov_x + cov_w] = ctx.prev_frame[dst_y : dst_y + win_h, cov_x : cov_x + cov_w]
            cmds.append(pack_update_bg(cov_x, dst_y, cov_w, win_h))
        elif mv_x < 0:
            cov_x, cov_w = dst_x, -mv_x
            bg_frame[dst_y : dst_y + win_h, cov_x : cov_x + cov_w] = ctx.prev_frame[dst_y : dst_y + win_h, cov_x : cov_x + cov_w]
            cmds.append(pack_update_bg(cov_x, dst_y, cov_w, win_h))

        # Covered Y strip (non-overlapping: width = win_w - abs(mv_x))
        if sub_w > 0:
            cov_sx = dst_x if mv_x >= 0 else dst_x + abs_mx
            if mv_y > 0:
                cov_y, cov_h = src_y + win_h, mv_y
                bg_frame[cov_y : cov_y + cov_h, cov_sx : cov_sx + sub_w] = ctx.prev_frame[cov_y : cov_y + cov_h, cov_sx : cov_sx + sub_w]
                cmds.append(pack_update_bg(cov_sx, cov_y, sub_w, cov_h))
            elif mv_y < 0:
                cov_y, cov_h = dst_y, -mv_y
                bg_frame[cov_y : cov_y + cov_h, cov_sx : cov_sx + sub_w] = ctx.prev_frame[cov_y : cov_y + cov_h, cov_sx : cov_sx + sub_w]
                cmds.append(pack_update_bg(cov_sx, cov_y, sub_w, cov_h))

        # 2) Shift the entire window/panel locally on the Viewer GPU (0 KB image data!)
        cmds.append(pack_transform_rect(
            src_x, src_y, win_w, win_h,
            dst_x, dst_y, win_w, win_h
        ))

        # 3) Restore uncovered strips from Background Surface (0 KB!) or StateCache / JPEG
        bg_restores = 0
        cache_hits = 0

        # Uncovered X strip
        if mv_x > 0:
            unc_x, unc_w = src_x, mv_x
            s_cmds, kind = self._restore_or_encode_strip(ctx, bg_frame, unc_x, src_y, unc_w, win_h)
            cmds.extend(s_cmds)
            if kind == "bg":
                bg_restores += 1
            elif kind == "cache":
                cache_hits += 1
        elif mv_x < 0:
            unc_x, unc_w = dst_x + win_w, -mv_x
            s_cmds, kind = self._restore_or_encode_strip(ctx, bg_frame, unc_x, src_y, unc_w, win_h)
            cmds.extend(s_cmds)
            if kind == "bg":
                bg_restores += 1
            elif kind == "cache":
                cache_hits += 1

        # Uncovered Y strip (non-overlapping: width = win_w - abs(mv_x))
        if sub_w > 0:
            unc_sx = src_x + mv_x if mv_x >= 0 else src_x
            if mv_y > 0:
                unc_y, unc_h = src_y, mv_y
                s_cmds, kind = self._restore_or_encode_strip(ctx, bg_frame, unc_sx, unc_y, sub_w, unc_h)
                cmds.extend(s_cmds)
                if kind == "bg":
                    bg_restores += 1
                elif kind == "cache":
                    cache_hits += 1
            elif mv_y < 0:
                unc_y, unc_h = dst_y + win_h, -mv_y
                s_cmds, kind = self._restore_or_encode_strip(ctx, bg_frame, unc_sx, unc_y, sub_w, unc_h)
                cmds.extend(s_cmds)
                if kind == "bg":
                    bg_restores += 1
                elif kind == "cache":
                    cache_hits += 1

        # 4) Patch rounded window corners / shadows / stationary cursor inside overlap
        for rx, ry, rw, rh in residual_rects:
            res_patch = ctx.curr_frame[ry:ry + rh, rx:rx + rw]
            cmds.append(pack_jpeg_patch(rx, ry, rw, rh, ctx.encode_jpeg(res_patch, ctx.quality)))

        ctx.prev_frame[y:y + h, x:x + w] = ctx.curr_frame[y:y + h, x:x + w]

        est_saved = max(1024, int(win_w * win_h * 0.12))
        ctx.shared["bytes_saved"] += est_saved
        cmds.append(pack_overlay_meta({
            "event": "window_move",
            "dx": int(-mv_x),
            "dy": int(-mv_y),
            "viewport": [int(x), int(y), int(w), int(h)],
            "virtual_y": int(ctx.shared.get("virtual_y", 0)),
            "saved_kb": round(ctx.shared["bytes_saved"] / 1024.0, 1)
        }))
        if ctx.shared.get("debug_mode", True):
            logging.info(
                f"[VF-DEBUG][HOST] WINDOW_MOVE_2D rect=({x},{y},{w}x{h}) "
                f"move=({mv_x:+d},{mv_y:+d})px win=({win_w}x{win_h}) "
                f"bg_restores={bg_restores} cache_hits={cache_hits} residuals={len(residual_rects)} "
                f"saved_total={ctx.shared['bytes_saved'] / 1024.0:.1f}KB"
            )
        return cmds

    @staticmethod
    def _get_window_coords(x: int, y: int, w: int, h: int, mv_x: int, mv_y: int):
        if mv_x >= 0:
            src_win_x = x
            dst_win_x = x + mv_x
            win_w = w - mv_x
        else:
            src_win_x = x - mv_x
            dst_win_x = x
            win_w = w + mv_x

        if mv_y >= 0:
            src_win_y = y
            dst_win_y = y + mv_y
            win_h = h - mv_y
        else:
            src_win_y = y - mv_y
            dst_win_y = y
            win_h = h + mv_y

        return src_win_x, src_win_y, dst_win_x, dst_win_y, win_w, win_h

    @classmethod
    def _restore_or_encode_strip(cls, ctx: FrameContext, bg_frame: np.ndarray,
                                 sx: int, sy: int, sw: int, sh: int) -> tuple[list[bytes], str]:
        if sw <= 0 or sh <= 0:
            return [], ""
        strip_arr = ctx.curr_frame[sy:sy + sh, sx:sx + sw]
        known = ctx.shared["known_states"]
        dirty = ctx.shared["dirty_states"]

        # 1. Background Surface match (0 KB network transfer!)
        if bg_frame is not None and bg_frame.shape[:2] == ctx.curr_frame.shape[:2]:
            bg_sub = bg_frame[sy:sy + sh, sx:sx + sw]
            if bg_sub.shape == strip_arr.shape:
                diff = np.abs(strip_arr[::2, ::2, :3].astype(np.int16) - bg_sub[::2, ::2, :3].astype(np.int16)).max(axis=2)
                if float((diff <= 12).mean()) >= 0.94 and float(diff.mean()) <= 4.5:
                    ctx.shared["bytes_saved"] += max(512, int(sw * sh * 0.14))
                    return [pack_restore_bg(sx, sy, sw, sh)], "bg"

        # 2. Exact hash match from StateCache
        shash = fast_region_hash(strip_arr)
        if shash and shash in known and shash not in dirty and known[shash][2:] == (sw, sh):
            ctx.shared["bytes_saved"] += max(512, int(sw * sh * 0.12))
            known.move_to_end(shash)
            return [pack_restore_state(shash, sx, sy, sw, sh)], "cache"

        # 3. Fallback: Network JPEG patch + update background surface
        jpeg_bytes = ctx.encode_jpeg(strip_arr, ctx.quality)
        out = [pack_jpeg_patch(sx, sy, sw, sh, jpeg_bytes)]
        if shash:
            out.append(pack_save_state(shash, sx, sy, sw, sh))
            StateCacheRecognizer._commit_known(known, dirty, shash, (sx, sy, sw, sh))
        if bg_frame is not None and bg_frame.shape[:2] == ctx.curr_frame.shape[:2]:
            bg_frame[sy:sy + sh, sx:sx + sw] = strip_arr
            out.append(pack_update_bg(sx, sy, sw, sh))
        return out, "jpeg"

    @staticmethod
    def _detect_2d_translation(prev_roi: np.ndarray, curr_roi: np.ndarray):
        """
        Finds a 2D translation vector (mv_x, mv_y) where `dst = src + (mv_x, mv_y)`
        using FFT Phase Correlation in <0.15ms.
        """
        h, w = prev_roi.shape[:2]
        if h < 40 or w < 40:
            return None

        p_gray = prev_roi[:, :, 1].astype(np.float32)
        c_gray = curr_roi[:, :, 1].astype(np.float32)

        try:
            shift, resp = cv2.phaseCorrelate(p_gray, c_gray)
            if resp < 0.25:
                return None
            mv_x = int(round(shift[0]))
            mv_y = int(round(shift[1]))
            return mv_x, mv_y
        except Exception:
            return None


class AnimationCycleRecognizer(BaseRecognizer):
    """
    Detects recurring UI animations (e.g. 'Working...' text shimmer, Progress Circle spinners,
    pulsing dots, indeterminate progress bars) where consecutive frame deltas are spatially
    adjacent or tightly interwoven ('benachbart oder eng verwoben'):
    1. Merges adjacent/overlapping small dirty rects across consecutive frames into a
       4px-padded Canonical Animation Bounding Box `U = (ux, uy, uw, uh)`.
    2. Stores area-averaged 16x16 perceptual thumbnails + Viewer RAM cache hashes for up to
       64 phases of `U`.
    3. Detects recursion/cycles via perceptual matching and replays the animation from the
       Viewer's RAM cache with 0 KB JPEG transfer!
    """
    name = "AnimationCycleRecognizer"

    @staticmethod
    def _make_thumb(roi: np.ndarray) -> tuple[np.ndarray, float]:
        bgr = roi[:, :, :3]
        std_val = float(bgr.std())
        if HAS_CV2 and bgr.shape[0] >= 8 and bgr.shape[1] >= 8:
            th = cv2.resize(bgr, (16, 16), interpolation=cv2.INTER_AREA).astype(np.int16)
        else:
            th = bgr[::2, ::2].astype(np.int16)
        return th, std_val

    def analyze(self, ctx: FrameContext):
        x, y, w, h = ctx.dirty_rect
        # Only target compact UI animation regions (spinners, loading wheels, pulsing badges, progress bars)
        if w < 12 or h < 12:
            return None
        if not ((w <= 96 and h <= 96) or (w <= 280 and h <= 36)):
            return None

        fh, fw = ctx.curr_frame.shape[:2]
        now = time.time()
        hotspots: list = ctx.shared.setdefault("anim_hotspots", [])
        # Prune expired hotspots (inactive for > 3.0 seconds)
        hotspots[:] = [hs for hs in hotspots if (now - hs["last_ts"]) <= 3.0]

        known: collections.OrderedDict = ctx.shared["known_states"]
        dirty_set: set = ctx.shared["dirty_states"]

        # Find an existing hotspot that overlaps or is tightly adjacent (within 8px) to dirty_rect
        matched_hs = None
        margin = 8
        for hs in hotspots:
            hx, hy, hw, hh = hs["rect"]
            if (x <= hx + hw + margin and x + w + margin >= hx and
                    y <= hy + hh + margin and y + h + margin >= hy):
                matched_hs = hs
                break

        if matched_hs is None:
            px1 = max(0, (int(x) - 2) & ~3)
            py1 = max(0, (int(y) - 2) & ~3)
            px2 = min(fw, (int(x + w) + 5) & ~3)
            py2 = min(fh, (int(y + h) + 5) & ~3)
            hotspots.append({
                "rect": [px1, py1, px2 - px1, py2 - py1],
                "hits": 1,
                "last_ts": now,
                "last_hit_hash": None,
                "keyframes": [],  # list of (khash, krect, kthumb, kstd, kroi)
            })
            if len(hotspots) > 16:
                hotspots.pop(0)
            return None

        # Check if dirty_rect (with 2px anti-aliasing margin) expands the hotspot's canonical union box U
        hx, hy, hw, hh = matched_hs["rect"]
        px1 = max(0, (int(x) - 2) & ~3)
        py1 = max(0, (int(y) - 2) & ~3)
        px2 = min(fw, (int(x + w) + 5) & ~3)
        py2 = min(fh, (int(y + h) + 5) & ~3)
        ux1 = min(px1, hx)
        uy1 = min(py1, hy)
        ux2 = max(px2, hx + hw)
        uy2 = max(py2, hy + hh)
        uw, uh = ux2 - ux1, uy2 - uy1
        if not ((uw <= 96 and uh <= 96) or (uw <= 280 and uh <= 36)):
            return None

        if (ux1, uy1, uw, uh) != (hx, hy, hw, hh):
            matched_hs["rect"] = [ux1, uy1, uw, uh]

        matched_hs["hits"] += 1
        matched_hs["last_ts"] = now

        # Activate animation cycle tracking once >= 2 updates occur in this hotspot
        if matched_hs["hits"] < 2:
            return None

        ux, uy, uw, uh = matched_hs["rect"]
        curr_u = ctx.curr_frame[uy:uy + uh, ux:ux + uw]
        prev_u = ctx.prev_frame[uy:uy + uh, ux:ux + uw]
        curr_u_thumb, curr_u_std = self._make_thumb(curr_u)
        prev_u_thumb, _ = self._make_thumb(prev_u)
        u_step_mad = float(np.abs(curr_u_thumb - prev_u_thumb).mean())

        # 1. Recursion / Cycle Check:
        best_hash = None
        best_rect = None
        best_mad = 999.0
        last_hit = matched_hs.get("last_hit_hash")
        valid_kf = []
        num_kf = len(matched_hs["keyframes"])

        for entry in matched_hs["keyframes"]:
            if len(entry) == 5:
                khash, krect, kthumb, kstd, kroi = entry
            else:
                khash, krect, kthumb, kstd = entry[:4]
                kroi = None

            if khash not in known or khash in dirty_set or known[khash] != krect:
                continue
            valid_kf.append((khash, krect, kthumb, kstd, kroi))
            if khash == last_hit:
                continue
            kx, ky, kw, kh = krect
            # Keyframe MUST match the current region dimensions (no giant keyframe swallowing small dirty rect!)
            if abs(kw - uw) > 8 or abs(kh - uh) > 8 or abs(kx - ux) > 8 or abs(ky - uy) > 8:
                continue

            c_sub, c_std = curr_u_thumb, curr_u_std
            step_mad = u_step_mad
            if c_sub.shape != kthumb.shape:
                continue
            diff = np.abs(c_sub - kthumb)
            mad = float(diff.mean())
            if mad < best_mad and mad < max(2.2, step_mad * 0.85):
                inlier = float((diff.max(axis=2) <= 14).mean())
                mean_shift = float(np.abs(c_sub.mean(axis=(0, 1)) - kthumb.mean(axis=(0, 1))).max())
                is_spinner = (kw <= 84 and kh <= 84 and abs(kw - kh) <= 28 and num_kf >= 8)
                std_ok = (kstd > 3.5 and 0.65 <= (c_std / kstd) <= 1.45) if kstd > 3.5 else (c_std <= 5.0)
                if mean_shift <= 10.0 and std_ok:
                    if (mad <= 5.2 and inlier >= 0.88) or (is_spinner and mad <= 8.5):
                        # Verify against full pixel ROI if available to guarantee zero visual corruption!
                        if kroi is not None and kroi.shape == curr_u.shape:
                            p_diff = np.abs(curr_u[::2, ::2, :3].astype(np.int16) - kroi[::2, ::2, :3].astype(np.int16)).max(axis=2)
                            if float((p_diff <= 14).mean()) < 0.90 or float(p_diff.mean()) > 4.5:
                                continue
                        best_mad = mad
                        best_hash = khash
                        best_rect = krect
        matched_hs["keyframes"] = valid_kf

        cmds = []
        if best_hash is not None and best_rect is not None:
            bx, by, bw, bh = best_rect
            # Cycle hit! Replay this animation frame directly from the Viewer's RAM cache (0 KB JPEG)
            ctx.shared["pending_pre_save"] = None
            ctx.shared["pending_post_save"] = None
            matched_hs["last_hit_hash"] = best_hash
            known.move_to_end(best_hash)
            cmds.append(pack_restore_state(best_hash, bx, by, bw, bh))
            est_saved = max(1024, int(bw * bh * 0.14))
            ctx.shared["bytes_saved"] += est_saved
            cmds.append(pack_overlay_meta({
                "event": "anim_cycle",
                "rect": [bx, by, bw, bh],
                "saved_kb": round(ctx.shared["bytes_saved"] / 1024.0, 1)
            }))
            if ctx.shared.get("debug_mode", True):
                logging.info(
                    f"[VF-DEBUG][HOST] ANIM_CYCLE_HIT hash={best_hash & 0xFFFFFFFF:08x} "
                    f"rect=({bx},{by},{bw}x{bh}) mad={best_mad:.2f} "
                    f"keyframes={len(matched_hs['keyframes'])} saved={est_saved / 1024.0:.1f}KB"
                )
            ctx.prev_frame[by:by + bh, bx:bx + bw] = ctx.curr_frame[by:by + bh, bx:bx + bw]
            return cmds

        # 2. First pass of this animation phase: learn & cache the unified canonical box U = (ux, uy, uw, uh)
        prev_u = ctx.prev_frame[uy:uy + uh, ux:ux + uw]

        ctx.shared["pending_pre_save"] = None
        ctx.shared["pending_post_save"] = None

        if not matched_hs["keyframes"]:
            prev_hash = fast_region_hash(prev_u)
            if prev_hash and prev_hash not in known:
                cmds.append(pack_save_state(prev_hash, ux, uy, uw, uh))
                StateCacheRecognizer._commit_known(known, dirty_set, prev_hash, (ux, uy, uw, uh))
                prev_thumb, prev_std = self._make_thumb(prev_u)
                matched_hs["keyframes"].append((prev_hash, (ux, uy, uw, uh), prev_thumb, prev_std, prev_u.copy()))

        jpeg_bytes = ctx.encode_jpeg(curr_u, ctx.quality)
        cmds.append(pack_jpeg_patch(ux, uy, uw, uh, jpeg_bytes))

        curr_hash = fast_region_hash(curr_u)
        if curr_hash:
            cmds.append(pack_save_state(curr_hash, ux, uy, uw, uh))
            StateCacheRecognizer._commit_known(known, dirty_set, curr_hash, (ux, uy, uw, uh))
            matched_hs["keyframes"].append((curr_hash, (ux, uy, uw, uh), curr_u_thumb, curr_u_std, curr_u.copy()))
            matched_hs["last_hit_hash"] = curr_hash
            if len(matched_hs["keyframes"]) > 64:
                matched_hs["keyframes"].pop(0)

        if ctx.shared.get("debug_mode", True):
            logging.info(
                f"[VF-DEBUG][HOST] ANIM_LEARN_FRAME rect=({ux},{uy},{uw}x{uh}) "
                f"keyframes={len(matched_hs['keyframes'])} size={len(jpeg_bytes) / 1024.0:.1f}KB"
            )
        ctx.prev_frame[uy:uy + uh, ux:ux + uw] = curr_u
        return cmds


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
            AnimationCycleRecognizer(),
            Scroll2DAndSliderRecognizer(),
            WindowMove2DRecognizer(),
        ]
        self.prev_frame = None
        self.shared = {
            "known_states": collections.OrderedDict(),
            "dirty_states": set(),
            "anim_hotspots": [],
            "pending_cmds": [],
            "pending_pre_save": None,
            "pending_post_save": None,
            "virtual_y": 0,
            "virtual_x": 0,
            "dirty_below": False,
            "bytes_saved": 0,
            "bg_frame": None,
            "debug_mode": True,
        }

    def register_plugin(self, plugin: BaseRecognizer):
        self.plugins.append(plugin)

    def reset(self):
        self.prev_frame = None
        self.shared["bg_frame"] = None
        self.shared["known_states"].clear()
        self.shared["dirty_states"].clear()
        self.shared["anim_hotspots"].clear()
        self.shared["pending_cmds"].clear()
        self.shared["pending_pre_save"] = None
        self.shared["pending_post_save"] = None
        self.shared["virtual_y"] = 0
        self.shared["virtual_x"] = 0
        self.shared["dirty_below"] = False

    def notify_input(self, evt: dict):
        if evt.get("event") == "vision_debug":
            self.shared["debug_mode"] = bool(evt.get("enabled", True))
            logging.info(f"[VF-DEBUG][HOST] Debug logging toggled {'ON' if self.shared['debug_mode'] else 'OFF'}")
            return
        if evt.get("event") in ("key_down", "mouse_down"):
            self.shared["dirty_below"] = False
        for p in self.plugins:
            p.on_input_event(evt, self.shared)

    @staticmethod
    def _refine_rect(prev_frame: np.ndarray, curr_frame: np.ndarray,
                     rx1: int, ry1: int, rx2: int, ry2: int, fw: int, fh: int) -> tuple:
        """Refines a coarse 4x4-grid bounding box to exact 1:1 boundaries of actual pixel changes
        with 1px safety margin so anti-aliasing edges never get clipped."""
        sx1, sy1 = max(0, rx1 - 4), max(0, ry1 - 4)
        sx2, sy2 = min(fw, rx2 + 4), min(fh, ry2 + 4)
        sub_p = prev_frame[sy1:sy2, sx1:sx2, :3]
        sub_c = curr_frame[sy1:sy2, sx1:sx2, :3]
        m = np.abs(sub_p.astype(np.int16) - sub_c.astype(np.int16)).max(axis=2) > 3
        ys, xs = np.nonzero(m)
        if ys.size == 0:
            return (int(rx1), int(ry1), max(12, int(rx2 - rx1)), max(12, int(ry2 - ry1)))
        px1 = max(0, sx1 + int(xs.min()) - 1)
        py1 = max(0, sy1 + int(ys.min()) - 1)
        px2 = min(fw, sx1 + int(xs.max()) + 2)
        py2 = min(fh, sy1 + int(ys.max()) + 2)
        w, h = max(12, px2 - px1), max(12, py2 - py1)
        px2, py2 = min(fw, px1 + w), min(fh, py1 + h)
        return (int(px1), int(py1), int(px2 - px1), int(py2 - py1))

    @classmethod
    def _extract_dirty_rects(cls, prev_frame: np.ndarray, curr_frame: np.ndarray,
                             mask: np.ndarray, base_gx: int, base_gy: int,
                             fw: int, fh: int, bridge_cells: int = 2, max_rects: int = 8) -> list[tuple]:
        """
        Splits a 4x4-grid boolean change mask into pixel-tight connected-component bounding boxes.
        Bridges gaps up to `bridge_cells * 4` pixels so characters/borders of a single UI control
        stay grouped together while separate buttons, tabs, or cursors become independent rects.
        """
        ys, xs = np.nonzero(mask)
        if ys.size == 0:
            return []

        def _make_rect(c_xs, c_ys, off_gx, off_gy):
            off_gx, off_gy = int(off_gx), int(off_gy)
            rx1 = max(0, (off_gx + int(c_xs.min())) * 4)
            ry1 = max(0, (off_gy + int(c_ys.min())) * 4)
            rx2 = min(int(fw), (off_gx + int(c_xs.max()) + 1) * 4)
            ry2 = min(int(fh), (off_gy + int(c_ys.max()) + 1) * 4)
            return cls._refine_rect(prev_frame, curr_frame, rx1, ry1, rx2, ry2, int(fw), int(fh))

        if HAS_CV2 and mask.shape[0] >= 4 and mask.shape[1] >= 4:
            ksize = 2 * bridge_cells + 1
            kernel = np.ones((ksize, ksize), dtype=np.uint8)
            dilated = cv2.dilate(mask.astype(np.uint8), kernel)
            num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(dilated, connectivity=8)
            if num_labels > 1:
                rects = []
                for lid in range(1, num_labels):
                    gx1, gy1, gw, gh = int(stats[lid, 0]), int(stats[lid, 1]), int(stats[lid, 2]), int(stats[lid, 3])
                    sub_m = mask[gy1:gy1 + gh, gx1:gx1 + gw] & (labels[gy1:gy1 + gh, gx1:gx1 + gw] == lid)
                    c_ys, c_xs = np.nonzero(sub_m)
                    if c_ys.size > 0:
                        rects.append(_make_rect(c_xs, c_ys, base_gx + gx1, base_gy + gy1))
                if rects:
                    # If there are more islands than max_rects, merge smallest islands into their nearest
                    # neighbor rather than collapsing the entire screen into a single giant bounding box!
                    while len(rects) > max_rects:
                        rects.sort(key=lambda r: r[2] * r[3], reverse=True)
                        sx, sy, sw, sh = rects.pop()
                        best_idx = 0
                        best_cost = 1 << 60
                        for idx, ( tx, ty, tw, th) in enumerate(rects):
                            ux1, uy1 = min(sx, tx), min(sy, ty)
                            ux2, uy2 = max(sx + sw, tx + tw), max(sy + sh, ty + th)
                            cost = (ux2 - ux1) * (uy2 - uy1) - (tw * th)
                            if cost < best_cost:
                                best_cost = cost
                                best_idx = idx
                        tx, ty, tw, th = rects[best_idx]
                        ux1, uy1 = min(sx, tx), min(sy, ty)
                        ux2, uy2 = max(sx + sw, tx + tw), max(sy + sh, ty + th)
                        rects[best_idx] = (ux1, uy1, ux2 - ux1, uy2 - uy1)
                    return rects

        return [_make_rect(xs, ys, base_gx, base_gy)]

    def encode_frame(self, curr_frame: np.ndarray, force: bool, encode_jpeg_fn, quality: int):
        """
        Single entry point called by `ScreenCapturer.capture_frame`.
        Detects dirty islands on a 4x4 grid, runs registered Vision/Memory plugins
        (hierarchical Macro-Island for Scroll/Panels -> Micro-Islands for Buttons/Hovers),
        and returns a `b"VF"` vision packet or full keyframe.
        """
        fh, fw = curr_frame.shape[:2]
        # Note: `force=True` from input events wakes up the capture grabber, but must NOT wipe out
        # delta encoding when `self.prev_frame` is already valid (connection/quality resets call `reset()`).
        if self.prev_frame is None or self.prev_frame.shape != curr_frame.shape:
            self.prev_frame = curr_frame.copy()
            self.shared["bg_frame"] = curr_frame.copy()
            return encode_jpeg_fn(curr_frame, quality)

        # Fast 4x4 grid diff with 3-LSB noise floor to ignore invisible DWM dithering
        g_prev = self.prev_frame[::4, ::4, :3]
        g_curr = curr_frame[::4, ::4, :3]
        mask = np.abs(g_prev.astype(np.int16) - g_curr.astype(np.int16)).max(axis=2) > 3
        if not np.any(mask):
            return None

        # Step 1: Extract Macro Islands (32px gap bridging keeps scrolling pages & dialogs intact)
        macro_rects = self._extract_dirty_rects(self.prev_frame, curr_frame, mask, 0, 0, fw, fh, bridge_cells=8, max_rects=8)
        all_cmds = []
        micro_count = 0

        for m_rect in macro_rects:
            mx, my, mw, mh = m_rect

            # If Macro Island is large enough to be a Scroll viewport or full Tab switch, test plugins on it first
            if mw >= 160 and mh >= 120:
                self.shared["pending_cmds"].clear()
                self.shared["pending_pre_save"] = None
                self.shared["pending_post_save"] = None
                ctx = FrameContext(self.prev_frame, curr_frame, m_rect, encode_jpeg_fn, quality, self.shared)
                handled = False
                for plugin in self.plugins:
                    try:
                        res_cmds = plugin.analyze(ctx)
                        if res_cmds:
                            all_cmds.extend(res_cmds)
                            handled = True
                            break
                    except Exception:
                        continue
                if handled:
                    micro_count += 1
                    continue

            # Step 2: Not a scroll or whole-panel cache hit -> split Macro Island into tight Micro Islands
            # (8px gap bridging isolates individual hovered buttons, menu items, icons, and cursors)
            gy1, gy2 = my // 4, min(mask.shape[0], (my + mh + 3) // 4)
            gx1, gx2 = mx // 4, min(mask.shape[1], (mx + mw + 3) // 4)
            sub_mask = mask[gy1:gy2, gx1:gx2]
            micro_rects = self._extract_dirty_rects(self.prev_frame, curr_frame, sub_mask, gx1, gy1, fw, fh, bridge_cells=2, max_rects=8)

            for u_rect in micro_rects:
                micro_count += 1
                u_cmds = self._process_single_rect(
                    self.prev_frame, curr_frame, u_rect, encode_jpeg_fn, quality,
                    run_plugins=(u_rect != m_rect or mw < 160 or mh < 120)
                )
                if u_cmds is None:
                    # Single massive un-cached screen change (>= 75% of screen area) -> send full keyframe
                    self.prev_frame = curr_frame.copy()
                    self.shared["bg_frame"] = curr_frame.copy()
                    return encode_jpeg_fn(curr_frame, quality)
                all_cmds.extend(u_cmds)

        if all_cmds:
            if micro_count > 1 and self.shared.get("debug_mode", True):
                logging.info(f"[VF-DEBUG][HOST] MULTI_ISLAND split into {micro_count} independent regions")
            return pack_vf_packet(all_cmds)

        return None

    def _process_single_rect(self, prev_frame: np.ndarray, curr_frame: np.ndarray,
                             dirty_rect: tuple, encode_jpeg_fn, quality: int, run_plugins: bool = True):
        """
        Runs registered Recognizer plugins on a single `dirty_rect` and returns a list of
        VF binary command chunks, or `None` if the rect covers >= 75% of the screen.
        """
        x, y, w, h = dirty_rect
        sub_p = prev_frame[y:y + h:2, x:x + w:2, :3]
        sub_c = curr_frame[y:y + h:2, x:x + w:2, :3]
        if not np.any(np.abs(sub_p.astype(np.int16) - sub_c.astype(np.int16)).max(axis=2) > 3):
            return []

        if run_plugins:
            self.shared["pending_cmds"].clear()
            self.shared["pending_pre_save"] = None
            self.shared["pending_post_save"] = None
            ctx = FrameContext(prev_frame, curr_frame, dirty_rect, encode_jpeg_fn, quality, self.shared)

            for plugin in self.plugins:
                try:
                    result_cmds = plugin.analyze(ctx)
                    if result_cmds:
                        return result_cmds
                except Exception:
                    continue

        x, y, w, h = dirty_rect
        if w * h >= curr_frame.shape[0] * curr_frame.shape[1] * 0.75:
            return None

        roi = curr_frame[y:y + h, x:x + w]
        jpeg_bytes = encode_jpeg_fn(roi, quality)
        cmds = []

        pre = self.shared.get("pending_pre_save")
        if pre:
            phash, px, py, pw, ph = pre
            cmds.append(pack_save_state(phash, px, py, pw, ph))
            StateCacheRecognizer._commit_known(self.shared["known_states"], self.shared["dirty_states"], phash, (px, py, pw, ph))
            self.shared["pending_pre_save"] = None

        cmds.extend(self.shared["pending_cmds"])
        self.shared["pending_cmds"].clear()
        cmds.append(pack_jpeg_patch(x, y, w, h, jpeg_bytes))

        post = self.shared.get("pending_post_save")
        if post:
            chash, sx, sy, sw, sh = post
            cmds.append(pack_save_state(chash, sx, sy, sw, sh))
            StateCacheRecognizer._commit_known(self.shared["known_states"], self.shared["dirty_states"], chash, (sx, sy, sw, sh))
            self.shared["pending_post_save"] = None

        if self.shared.get("debug_mode", True):
            logging.info(
                f"[VF-DEBUG][HOST] JPEG_PATCH rect=({x},{y},{w}x{h}) "
                f"size={len(jpeg_bytes) / 1024.0:.1f}KB"
            )

        prev_frame[y:y + h, x:x + w] = roi
        return cmds

    def process_delta(self, prev_frame: np.ndarray, curr_frame: np.ndarray,
                      dirty_rect: tuple, encode_jpeg_fn, quality: int):
        """Backwards-compatible wrapper around `_process_single_rect`."""
        cmds = self._process_single_rect(prev_frame, curr_frame, dirty_rect, encode_jpeg_fn, quality)
        return pack_vf_packet(cmds) if cmds else None


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


def pack_update_bg(sx: int, sy: int, sw: int, sh: int) -> bytes:
    return struct.pack(">BHHHH", CMD_UPDATE_BG, sx, sy, sw, sh)


def pack_restore_bg(dx: int, dy: int, dw: int, dh: int) -> bytes:
    return struct.pack(">BHHHH", CMD_RESTORE_BG, dx, dy, dw, dh)


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
        self.stats_bg_restores = 0
        self.stats_jpeg_patches = 0
        self.bg_surface = None

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
        self.bg_surface = None

    def toggle_debug(self, send_input_fn=None) -> bool:
        self.debug_mode = not self.debug_mode
        if not self.debug_mode:
            self.debug_rects.clear()
        self.last_event_ts = time.time()
        logging.info(f"[VF-DEBUG][VIEWER] Surface debug mode toggled {'ON' if self.debug_mode else 'OFF'}")
        if send_input_fn:
            try:
                send_input_fn({"event": "vision_debug", "enabled": self.debug_mode})
            except Exception:
                pass
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

    def on_full_frame(self, byte_len: int = 0, width: int = 1920, height: int = 1080, base_qimage=None):
        """Called when a full JPEG keyframe arrives."""
        self.stats_jpeg_patches += 1
        if base_qimage is not None and not base_qimage.isNull():
            self.bg_surface = base_qimage.copy()
        else:
            self.bg_surface = None
        kb = f"{byte_len / 1024.0:.1f} KB" if byte_len else "Keyframe"
        self._record_debug(0, 0, width, height, "full", f"FULL FRAME ({kb})")
        if self.debug_mode:
            logging.info(
                f"[VF-DEBUG][VIEWER] FULL_KEYFRAME ({width}x{height} {kb}) || "
                f"stats: hits={self.stats_cache_hits} scroll={self.stats_scroll_blits} "
                f"bg={self.stats_bg_restores} jpeg={self.stats_jpeg_patches} ram={len(self.state_cache)}"
            )

    def on_dl_patch(self, dx: int, dy: int, dw: int, dh: int, byte_len: int = 0):
        """Called when a standard `b"DL"` JPEG delta patch arrives."""
        self.stats_jpeg_patches += 1
        kb = f"{byte_len / 1024.0:.1f} KB" if byte_len else "JPEG"
        self._record_debug(dx, dy, dw, dh, "jpeg", f"NET JPEG ({kb})")
        if self.debug_mode and (dw * dh >= 120 * 80):
            logging.info(
                f"[VF-DEBUG][VIEWER] DL_PATCH rect=({dx},{dy},{dw}x{dh}) size={kb} || "
                f"stats: hits={self.stats_cache_hits} scroll={self.stats_scroll_blits} "
                f"bg={self.stats_bg_restores} jpeg={self.stats_jpeg_patches} ram={len(self.state_cache)}"
            )

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
        log_ops = []

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
                        while len(self.state_cache) > 256:
                            ev_k, _ = self.state_cache.popitem(last=False)
                            self.dirty_hashes.discard(ev_k)
                        if self.debug_mode:
                            log_ops.append(f"SAVE#{shash & 0xFFFF:04x}({sx},{sy},{sw}x{sh})")

                elif cmd == CMD_RESTORE_STATE and offset + 16 <= n:
                    shash, dx, dy, dw, dh = struct.unpack_from(">QHHHH", data, offset)
                    offset += 16
                    cached_img = self.state_cache.get(shash)
                    if cached_img is not None and not cached_img.isNull():
                        p.drawImage(qrectf_cls(dx, dy, dw, dh), cached_img)
                        self.state_cache.move_to_end(shash)
                        self.stats_cache_hits += 1
                        self._record_debug(dx, dy, dw, dh, "cache", "RAM CACHE (0 KB)")
                        if self.debug_mode:
                            log_ops.append(f"CACHE_HIT#{shash & 0xFFFF:04x}({dx},{dy},{dw}x{dh})")
                    else:
                        self._record_debug(dx, dy, dw, dh, "miss", "CACHE MISS!")
                        logging.warning(
                            f"[VF-DEBUG][VIEWER] CACHE_MISS hash={shash & 0xFFFFFFFF:08x} rect=({dx},{dy},{dw}x{dh})"
                        )
                        log_ops.append(f"CACHE_MISS#{shash & 0xFFFF:04x}({dx},{dy},{dw}x{dh})")

                elif cmd == CMD_TRANSFORM_RECT and offset + 16 <= n:
                    sx, sy, sw, sh, dx, dy, dw, dh = struct.unpack_from(">HHHHHHHH", data, offset)
                    offset += 16
                    src_copy = base_qimage.copy(sx, sy, sw, sh)
                    if not src_copy.isNull():
                        p.drawImage(qrectf_cls(dx, dy, dw, dh), src_copy)
                        self.stats_scroll_blits += 1
                        shift_y = sy - dy
                        shift_x = sx - dx
                        if shift_x and shift_y:
                            lbl = f"GPU MOVE (dx:{-shift_x:+d},dy:{-shift_y:+d}px, 0 KB)"
                        elif shift_y:
                            lbl = f"GPU SCROLL (dy:{shift_y:+d}px, 0 KB)"
                        else:
                            lbl = f"GPU SCROLL (dx:{shift_x:+d}px, 0 KB)"
                        self._record_debug(dx, dy, dw, dh, "scroll", lbl)
                        if self.debug_mode:
                            log_ops.append(f"SCROLL({sx},{sy},{sw}x{sh})->({dx},{dy}) dy={shift_y:+d} dx={shift_x:+d}")

                elif cmd == CMD_MARK_DIRTY and offset + 9 <= n:
                    shash, flags = struct.unpack_from(">QB", data, offset)
                    offset += 9
                    if shash:
                        self.dirty_hashes.add(shash)
                    if flags & 1:
                        self.dirty_below = True
                    if self.debug_mode:
                        log_ops.append(f"MARK_DIRTY(flags={flags})")

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
                            if self.debug_mode:
                                log_ops.append(f"JPEG({dx},{dy},{dw}x{dh} {jlen / 1024.0:.1f}KB)")

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

                elif cmd == CMD_UPDATE_BG and offset + 8 <= n:
                    sx, sy, sw, sh = struct.unpack_from(">HHHH", data, offset)
                    offset += 8
                    if self.bg_surface is None or self.bg_surface.size() != base_qimage.size():
                        self.bg_surface = base_qimage.copy()
                    else:
                        bg_p = qpainter_cls(self.bg_surface)
                        try:
                            bg_p.drawImage(qrectf_cls(sx, sy, sw, sh), base_qimage, qrectf_cls(sx, sy, sw, sh))
                        finally:
                            bg_p.end()
                    if self.debug_mode:
                        log_ops.append(f"BG_UPDATE({sx},{sy},{sw}x{sh})")

                elif cmd == CMD_RESTORE_BG and offset + 8 <= n:
                    dx, dy, dw, dh = struct.unpack_from(">HHHH", data, offset)
                    offset += 8
                    if self.bg_surface is not None and not self.bg_surface.isNull():
                        p.drawImage(qrectf_cls(dx, dy, dw, dh), self.bg_surface, qrectf_cls(dx, dy, dw, dh))
                        self.stats_bg_restores += 1
                        self._record_debug(dx, dy, dw, dh, "bg", "BG RESTORE (0 KB)")
                        if self.debug_mode:
                            log_ops.append(f"BG_RESTORE({dx},{dy},{dw}x{dh})")
                    else:
                        self._record_debug(dx, dy, dw, dh, "miss", "BG MISS!")
                        if self.debug_mode:
                            log_ops.append(f"BG_MISS({dx},{dy},{dw}x{dh})")
                else:
                    break
        finally:
            p.end()

        if self.debug_mode and log_ops:
            logging.info(
                f"[VF-DEBUG][VIEWER] pkt={len(data) / 1024.0:.1f}KB: {' | '.join(log_ops)} || "
                f"stats: hits={self.stats_cache_hits} scroll={self.stats_scroll_blits} "
                f"jpeg={self.stats_jpeg_patches} ram={len(self.state_cache)} "
                f"vY={self.virtual_y:+d}px saved={self.saved_kb:.1f}KB"
            )

    def _update_meta(self, meta: dict):
        self.last_event = meta.get("event", "")
        self.last_event_ts = time.time()
        if self.last_event == "anim_cycle" and self.debug_rects:
            self.debug_rects[-1]["label"] = "ANIM LOOP (0 KB)"
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
                "bg":     (QColor(6, 182, 212, 70),   QColor(34, 211, 238, 235),  QColor(21, 94, 117, 225)),   # CYAN:  Background Surface Restore
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
            leg_w = min(img_w - 28.0, 740.0)
            leg_h = 28.0
            leg_rect = QRectF(img_x + 14.0, img_y + 14.0, leg_w, leg_h)
            painter.setPen(QColor(56, 189, 248, 150))
            painter.setBrush(QColor(15, 23, 42, 225))
            painter.drawRoundedRect(leg_rect, 7.0, 7.0)

            legend_txt = (
                f"🔬 DEBUG  |  🟢 Cache ({self.stats_cache_hits})   "
                f"🔵 GPU-Move ({self.stats_scroll_blits})   "
                f"🔷 BG-Restore ({self.stats_bg_restores})   "
                f"🔴 Net-JPEG ({self.stats_jpeg_patches})   "
                f"📦 RAM: {len(self.state_cache)}"
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
            self.toggle_debug(send_input_fn)
            if update_fn:
                update_fn()
            return True

        if self.btn_bookmark_rect and self.btn_bookmark_rect.contains(pos_x, pos_y):
            self.bookmarked_y = self.virtual_y
            self.dirty_below = False
            self.last_event_ts = time.time()
            if self.debug_mode:
                logging.info(f"[VF-DEBUG][VIEWER] Bookmark set at vY={self.bookmarked_y:+d}px")
            if update_fn:
                update_fn()
            return True

        if self.btn_jump_rect and self.btn_jump_rect.contains(pos_x, pos_y):
            diff_y = self.virtual_y - self.bookmarked_y
            if abs(diff_y) >= 20:
                ticks = max(1, min(25, round(abs(diff_y) / 90.0)))
                delta_dir = 1 if diff_y > 0 else -1
                if self.debug_mode:
                    logging.info(
                        f"[VF-DEBUG][VIEWER] Auto-scroll back from vY={self.virtual_y:+d}px "
                        f"to bookmark={self.bookmarked_y:+d}px ({ticks} wheel ticks)"
                    )
                for _ in range(ticks):
                    send_input_fn({"event": "mouse_wheel", "delta": delta_dir})
                self.virtual_y = self.bookmarked_y
                self.last_event_ts = time.time()
            if update_fn:
                update_fn()
            return True

        return False
