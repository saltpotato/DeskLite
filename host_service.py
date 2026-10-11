import asyncio
import json
import logging
import threading
import time
import websockets
from capturer import ScreenCapturer
from input_controller import InputController
from common import get_clipboard_text, set_clipboard_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [HOST] %(message)s")

class HostService:
    def __init__(self, relay_url, my_id, my_password, quality=55, max_fps=30, scale=0.75, status_callback=None):
        self.relay_url = relay_url
        self.my_id = my_id
        self.my_password = my_password
        self.quality = quality
        self.max_fps = max_fps
        self.scale = scale
        self.status_callback = status_callback
        
        self.running = False
        self.active_viewers = 0
        self.force_frame = False
        self.last_clipboard = ""
        self.thread = None
        self.loop = None
        
        self.capturer = ScreenCapturer(quality=self.quality, scale=self.scale)
        self.input_controller = InputController(self.capturer.width, self.capturer.height, self.capturer.left, self.capturer.top)

    def set_status(self, status, detail=""):
        if self.status_callback:
            self.status_callback(status, detail)

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_thread, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if hasattr(self, "current_ws") and self.current_ws:
            try:
                if self.loop and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.current_ws.close(), self.loop)
            except Exception:
                pass
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

    def _run_thread(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        while self.running:
            try:
                self.loop.run_until_complete(self._connect_and_serve())
            except Exception as e:
                logging.warning(f"Verbindungsunterbrechung zum Relay: {e}")
                self.set_status("disconnected", str(e))
                time.sleep(2) # Auto-reconnect after 2 seconds

    async def _stream_frames(self, ws):
        interval = 1.0 / max(1, self.max_fps)
        consecutive_errors = 0
        while self.running and self.active_viewers > 0:
            start_t = time.time()
            try:
                force = self.force_frame
                self.force_frame = False
                frame_bytes = self.capturer.capture_frame(draw_cursor=False, force=force)
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                logging.debug(f"Capture-Fehler (vorübergehend): {e}")
                frame_bytes = None
                if consecutive_errors > 30:
                    break
                await asyncio.sleep(0.1)
                continue

            if frame_bytes is not None:
                try:
                    await ws.send(frame_bytes)
                except Exception as e:
                    logging.debug(f"Stream-Netzwerkfehler: {e}")
                    break
            
            elapsed = time.time() - start_t
            sleep_t = interval - elapsed
            if sleep_t > 0:
                await asyncio.sleep(sleep_t)
            else:
                await asyncio.sleep(0.001)

    async def _sync_clipboard(self, ws):
        while self.running and self.active_viewers > 0:
            try:
                cur = get_clipboard_text()
                if cur and cur != self.last_clipboard and len(cur) < 500000:
                    self.last_clipboard = cur
                    await ws.send(json.dumps({"type": "clipboard", "text": cur}))
            except Exception:
                pass
            await asyncio.sleep(0.8)

    async def _connect_and_serve(self):
        self.set_status("connecting", "Verbinde mit Relay-Server...")
        async with websockets.connect(
            self.relay_url,
            max_size=20 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=40
        ) as ws:
            self.current_ws = ws
            # Register Host
            reg_payload = {
                "action": "register",
                "id": self.my_id,
                "password": self.my_password
            }
            await ws.send(json.dumps(reg_payload))
            
            # Wait for registration confirmation
            resp_str = await ws.recv()
            resp = json.loads(resp_str)
            if resp.get("status") != "registered":
                self.set_status("error", resp.get("message", "Registrierung fehlgeschlagen"))
                return
            
            logging.info(f"Erfolgreich als Host '{self.my_id}' registriert!")
            self.set_status("online", f"Bereit für Verbindungen (ID: {self.my_id})")

            stream_task = None
            clip_task = None

            async for message in ws:
                if isinstance(message, str):
                    try:
                        data = json.loads(message)
                        msg_type = data.get("type")
                        
                        if msg_type == "viewer_connected":
                            self.active_viewers += 1
                            logging.info(f"Viewer verbunden! Aktive Viewer: {self.active_viewers}")
                            self.set_status("busy", f"Fernsteuerung aktiv ({self.active_viewers} Viewer)")
                            # Send initial screen info
                            info = {
                                "type": "screen_info",
                                "width": self.capturer.width,
                                "height": self.capturer.height
                            }
                            await ws.send(json.dumps(info))

                            # Start streaming frames
                            if stream_task is None or stream_task.done():
                                stream_task = asyncio.create_task(self._stream_frames(ws))

                            # Start clipboard synchronization
                            if clip_task is None or clip_task.done():
                                clip_task = asyncio.create_task(self._sync_clipboard(ws))

                        elif msg_type == "viewer_disconnected":
                            self.active_viewers = max(0, self.active_viewers - 1)
                            logging.info(f"Viewer getrennt. Aktive Viewer: {self.active_viewers}")
                            if self.active_viewers == 0:
                                self.set_status("online", f"Bereit für Verbindungen (ID: {self.my_id})")
                                if stream_task and not stream_task.done():
                                    stream_task.cancel()
                                if clip_task and not clip_task.done():
                                    clip_task.cancel()

                        elif msg_type == "clipboard":
                            text = data.get("text", "")
                            if text and text != self.last_clipboard:
                                self.last_clipboard = text
                                set_clipboard_text(text)
                                logging.info("Zwischenablage vom Viewer übernommen.")

                        elif msg_type == "input":
                            evt_data = data.get("data", {})
                            self.input_controller.handle_event(evt_data)
                            self.force_frame = True
                            
                            # Auto-revive stream_task if it died unexpectedly during idle
                            if self.active_viewers > 0 and (stream_task is None or stream_task.done()):
                                logging.info("Reaktiviere Desktop-Stream nach Inaktivität...")
                                stream_task = asyncio.create_task(self._stream_frames(ws))

                        elif msg_type == "set_quality":
                            self.capturer.update_settings(
                                quality=data.get("quality"),
                                scale=data.get("scale")
                            )
                            self.force_frame = True
                            logging.info(f"Qualitätsprofil geändert: scale={self.capturer.scale}, q={self.capturer.quality}")

                    except Exception as e:
                        logging.warning(f"Fehler beim Verarbeiten von Steuerungsdaten: {e}")
