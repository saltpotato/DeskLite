import sys
import pynput
from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Key, Controller as KeyboardController
from common import ensure_desktop_access

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
        self.mouse = MouseController()
        self.keyboard = KeyboardController()

    def handle_event(self, event):
        ensure_desktop_access()
        evt_type = event.get("event")

        if evt_type == "mouse_move":
            nx = event.get("x", 0.0)
            ny = event.get("y", 0.0)
            target_x = self.left + int(nx * self.width)
            target_y = self.top + int(ny * self.height)
            self.mouse.position = (target_x, target_y)

        elif evt_type == "mouse_down":
            btn = BUTTONS.get(event.get("button", "left"), Button.left)
            self.mouse.press(btn)

        elif evt_type == "mouse_up":
            btn = BUTTONS.get(event.get("button", "left"), Button.left)
            self.mouse.release(btn)

        elif evt_type == "mouse_click":
            btn = BUTTONS.get(event.get("button", "left"), Button.left)
            self.mouse.click(btn, event.get("count", 1))

        elif evt_type == "mouse_wheel":
            dy = event.get("delta", 0)
            # pynput scroll expects dx, dy
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
            elif name == "explorer":
                self.keyboard.press(Key.cmd)
                self.keyboard.press('e')
                self.keyboard.release('e')
                self.keyboard.release(Key.cmd)
            elif name == "task_manager":
                self.keyboard.press(Key.ctrl)
                self.keyboard.press(Key.shift)
                self.keyboard.press(Key.esc)
                self.keyboard.release(Key.esc)
                self.keyboard.release(Key.shift)
                self.keyboard.release(Key.ctrl)
