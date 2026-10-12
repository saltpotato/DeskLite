import sys
import ctypes
import logging
from common import ensure_desktop_access, is_admin

try:
    import pynput
    from pynput.mouse import Button, Controller as MouseController
    from pynput.keyboard import Key, Controller as KeyboardController
    HAS_PYNPUT = True
except Exception as e:
    logging.error(f"Failed to import pynput: {e}")
    HAS_PYNPUT = False
    Button = None
    Key = None

SPECIAL_KEYS = {}
BUTTONS = {}
if HAS_PYNPUT:
    SPECIAL_KEYS = {
        "enter": Key.enter,
        "return": Key.enter,
        "backspace": Key.backspace,
        "tab": Key.tab,
        "space": Key.space,
        "shift": Key.shift,
        "ctrl": Key.ctrl,
        "control": Key.ctrl,
        "alt": Key.alt,
        "esc": Key.esc,
        "escape": Key.esc,
        "delete": Key.delete,
        "del": Key.delete,
        "home": Key.home,
        "end": Key.end,
        "page_up": Key.page_up,
        "pageup": Key.page_up,
        "page_down": Key.page_down,
        "pagedown": Key.page_down,
        "left": Key.left,
        "right": Key.right,
        "up": Key.up,
        "down": Key.down,
        "caps_lock": Key.caps_lock,
        "cmd": Key.cmd,
        "win": Key.cmd,
        "super": Key.cmd,
        "meta": Key.cmd,
        "f1": Key.f1, "f2": Key.f2, "f3": Key.f3, "f4": Key.f4,
        "f5": Key.f5, "f6": Key.f6, "f7": Key.f7, "f8": Key.f8,
        "f9": Key.f9, "f10": Key.f10, "f11": Key.f11, "f12": Key.f12
    }
    BUTTONS = {
        "left": Button.left,
        "right": Button.right,
        "middle": Button.middle
    }

class InputController:
    def __init__(self, screen_width, screen_height, screen_left=0, screen_top=0):
        ensure_desktop_access()
        self.width = screen_width
        self.height = screen_height
        self.left = screen_left
        self.top = screen_top
        self.mouse = None
        self.keyboard = None
        if HAS_PYNPUT:
            try:
                self.mouse = MouseController()
            except Exception as e:
                logging.warning(f"MouseController init warning: {e}")
            try:
                self.keyboard = KeyboardController()
            except Exception as e:
                logging.warning(f"KeyboardController init warning: {e}")

    def handle_event(self, event):
        ensure_desktop_access()
        if sys.platform != "win32" and (self.mouse is None or self.keyboard is None):
            return
        evt_type = event.get("event")

        if evt_type == "mouse_move":
            nx = event.get("x", 0.0)
            ny = event.get("y", 0.0)
            self.last_x = self.left + int(nx * self.width)
            self.last_y = self.top + int(ny * self.height)
            if sys.platform == "win32":
                ctypes.windll.user32.SetCursorPos(self.last_x, self.last_y)
                scr_w = max(1, ctypes.windll.user32.GetSystemMetrics(0))
                scr_h = max(1, ctypes.windll.user32.GetSystemMetrics(1))
                norm_x = int(self.last_x * 65535 / scr_w)
                norm_y = int(self.last_y * 65535 / scr_h)
                ctypes.windll.user32.mouse_event(0x8000 | 0x0001, norm_x, norm_y, 0, 0)
            else:
                self.mouse.position = (self.last_x, self.last_y)

        elif evt_type == "mouse_down":
            btn = event.get("button", "left")
            if sys.platform == "win32":
                # Ensure cursor position is synced right before click
                ctypes.windll.user32.SetCursorPos(getattr(self, "last_x", 0), getattr(self, "last_y", 0))
                flag = 0x0002 if btn == "left" else (0x0008 if btn == "right" else 0x0020)
                # Send with MOUSEEVENTF_ABSOLUTE to guarantee it hits the active elevated control
                scr_w = max(1, ctypes.windll.user32.GetSystemMetrics(0))
                scr_h = max(1, ctypes.windll.user32.GetSystemMetrics(1))
                norm_x = int(getattr(self, "last_x", 0) * 65535 / scr_w)
                norm_y = int(getattr(self, "last_y", 0) * 65535 / scr_h)
                ctypes.windll.user32.mouse_event(flag | 0x8000 | 0x0001, norm_x, norm_y, 0, 0)
            else:
                p_btn = BUTTONS.get(btn, Button.left)
                self.mouse.press(p_btn)

        elif evt_type == "mouse_up":
            btn = event.get("button", "left")
            if sys.platform == "win32":
                flag = 0x0004 if btn == "left" else (0x0010 if btn == "right" else 0x0040)
                scr_w = max(1, ctypes.windll.user32.GetSystemMetrics(0))
                scr_h = max(1, ctypes.windll.user32.GetSystemMetrics(1))
                norm_x = int(getattr(self, "last_x", 0) * 65535 / scr_w)
                norm_y = int(getattr(self, "last_y", 0) * 65535 / scr_h)
                ctypes.windll.user32.mouse_event(flag | 0x8000 | 0x0001, norm_x, norm_y, 0, 0)
            else:
                p_btn = BUTTONS.get(btn, Button.left)
                self.mouse.release(p_btn)

        elif evt_type == "mouse_click":
            btn = event.get("button", "left")
            count = event.get("count", 1)
            if sys.platform == "win32":
                ctypes.windll.user32.SetCursorPos(getattr(self, "last_x", 0), getattr(self, "last_y", 0))
                down_flag = 0x0002 if btn == "left" else (0x0008 if btn == "right" else 0x0020)
                up_flag = 0x0004 if btn == "left" else (0x0010 if btn == "right" else 0x0040)
                scr_w = max(1, ctypes.windll.user32.GetSystemMetrics(0))
                scr_h = max(1, ctypes.windll.user32.GetSystemMetrics(1))
                norm_x = int(getattr(self, "last_x", 0) * 65535 / scr_w)
                norm_y = int(getattr(self, "last_y", 0) * 65535 / scr_h)
                for _ in range(count):
                    ctypes.windll.user32.mouse_event(down_flag | 0x8000 | 0x0001, norm_x, norm_y, 0, 0)
                    ctypes.windll.user32.mouse_event(up_flag | 0x8000 | 0x0001, norm_x, norm_y, 0, 0)
            else:
                p_btn = BUTTONS.get(btn, Button.left)
                self.mouse.click(p_btn, count)

        elif evt_type == "mouse_wheel":
            dy = event.get("delta", 0)
            if sys.platform == "win32":
                # MOUSEEVENTF_WHEEL = 0x0800, 1 wheel tick = WHEEL_DELTA (120)
                ctypes.windll.user32.mouse_event(0x0800, 0, 0, int(dy * 120), 0)
            else:
                self.mouse.scroll(0, dy)

        elif evt_type == "key_down":
            key_name = event.get("key", "").lower()
            if key_name in SPECIAL_KEYS:
                self.keyboard.press(SPECIAL_KEYS[key_name])
            elif len(event.get("char", "")) > 0:
                self.keyboard.press(event["char"])

        elif evt_type == "key_up":
            key_name = event.get("key", "").lower()
            if key_name in SPECIAL_KEYS:
                self.keyboard.release(SPECIAL_KEYS[key_name])
            elif len(event.get("char", "")) > 0:
                self.keyboard.release(event["char"])

        elif evt_type == "shortcut":
            name = event.get("name")
            if name == "win_key":
                self.keyboard.press(Key.cmd)
                self.keyboard.release(Key.cmd)
            elif name == "desktop":
                # Minimize / Show Desktop (Win+D)
                self.keyboard.press(Key.cmd)
                self.keyboard.press('d')
                self.keyboard.release('d')
                self.keyboard.release(Key.cmd)
            elif name == "explorer":
                self.keyboard.press(Key.cmd)
                self.keyboard.press('e')
                self.keyboard.release('e')
                self.keyboard.release(Key.cmd)
            elif name == "task_manager":
                if is_admin():
                    self.keyboard.press(Key.ctrl)
                    self.keyboard.press(Key.shift)
                    self.keyboard.press(Key.esc)
                    self.keyboard.release(Key.esc)
                    self.keyboard.release(Key.shift)
                    self.keyboard.release(Key.ctrl)
