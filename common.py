import json
import os
import random
import ctypes
import sys

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

def ensure_desktop_access():
    """Ensures thread has access to active Windows desktop station."""
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            hdesk = user32.OpenInputDesktop(0, False, 0x01FF) # GENERIC_ALL
            if hdesk:
                user32.SetThreadDesktop(hdesk)
        except Exception:
            pass

def generate_id():
    """Generates a random 6-digit ID like 384 192."""
    return f"{random.randint(100, 999)} {random.randint(100, 999)}"

def generate_password():
    """Generates a random 6-character alphanumeric password."""
    chars = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(random.choices(chars, k=6))

def load_config():
    default_config = {
        "relay_server": "ws://localhost:8765",
        "my_id": generate_id(),
        "my_password": generate_password(),
        "quality": 65,
        "max_fps": 25,
        "scale": 1.0 # 1.0 = native resolution
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                default_config.update(saved)
        except Exception:
            pass
    else:
        save_config(default_config)
    return default_config

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass
