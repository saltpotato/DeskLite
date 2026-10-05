import asyncio
import json
import logging
import threading
import time
import websockets
from capturer import ScreenCapturer
from input_controller import InputController

logging.basicConfig(level=logging.INFO, format="%(asctime)s [HOST] %(message)s")

class HostService:
    def __init__(self, relay_url, my_id, my_password, quality=60, max_fps=30, scale=0.85, status_callback=None):
        self.relay_url = relay_url
        self.my_id = my_id
        self.my_password = my_password
        self.quality = quality
        self.max_fps = max_fps
        self.scale = scale
        self.status_callback = status_callback
        
        self.running = False
        self.active_viewers = 0
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
                time.sleep(3) # Wait before retry

    async def _stream_frames(self, ws):
        interval = 1.0 / max(1, self.max_fps)
        while self.running and self.active_viewers > 0:
            start_t = time.time()
            try:
                # draw_cursor=False: allows 0ms local hardware cursor on viewer!
                frame_bytes = self.capturer.capture_frame(draw_cursor=False)
                await ws.send(frame_bytes)
            except Exception as e:
                logging.debug(f"Stream-Fehler: {e}")
                break
            
            elapsed = time.time() - start_t
            sleep_t = interval - elapsed
            if sleep_t > 0:
                await asyncio.sleep(sleep_t)
            else:
                await asyncio.sleep(0.001)

    async def _connect_and_serve(self):
        self.set_status("connecting", "Verbinde mit Relay-Server...")
        async with websockets.connect(
            self.relay_url,
            max_size=20 * 1024 * 1024,
            ping_interval=15,
            ping_timeout=15
        ) as ws:
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

                            # Start streaming frames if not already streaming
                            if stream_task is None or stream_task.done():
                                stream_task = asyncio.create_task(self._stream_frames(ws))

                        elif msg_type == "viewer_disconnected":
                            self.active_viewers = max(0, self.active_viewers - 1)
                            logging.info(f"Viewer getrennt. Aktive Viewer: {self.active_viewers}")
                            if self.active_viewers == 0:
                                self.set_status("online", f"Bereit für Verbindungen (ID: {self.my_id})")
                                if stream_task and not stream_task.done():
                                    stream_task.cancel()

                        elif msg_type == "input":
                            # Process mouse / keyboard event
                            self.input_controller.handle_event(data.get("data", {}))

                    except Exception as e:
                        logging.warning(f"Fehler beim Verarbeiten von Steuerungsdaten: {e}")
