import asyncio
import json
import logging
import secrets
import socket
import struct
import threading
import time
import websockets
from capturer import ScreenCapturer
from input_controller import InputController
from common import get_clipboard_text, set_clipboard_text

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
    HAS_WEBRTC = True
except ImportError:
    HAS_WEBRTC = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [HOST] %(message)s")


def get_local_ipv4_addresses():
    """Returns a prioritized list of local LAN/VPN IPv4 addresses for direct P2P connections."""
    ips = []
    try:
        # Primary outbound interface IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            primary_ip = s.getsockname()[0]
            if primary_ip and not primary_ip.startswith("127."):
                ips.append(primary_ip)
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    ips.append("127.0.0.1")
    return ips


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
        self.relay_viewers = 0
        self.p2p_ws_clients = set()
        self.webrtc_channels = set()
        self.webrtc_pcs = set()
        self.p2p_server = None
        self.p2p_port = None
        self.p2p_token = secrets.token_hex(16)
        self.frame_seq = 0

        self.force_frame = False
        self.last_clipboard = ""
        self.thread = None
        self.loop = None
        self.current_ws = None
        self.stream_task = None
        self.clip_task = None

        self.capturer = ScreenCapturer(quality=self.quality, scale=self.scale)
        self.input_controller = InputController(
            self.capturer.width,
            self.capturer.height,
            self.capturer.left,
            self.capturer.top
        )

    @property
    def total_viewers(self):
        return self.relay_viewers + len(self.p2p_ws_clients) + len(self.webrtc_channels)

    @property
    def has_p2p_viewers(self):
        return (len(self.p2p_ws_clients) + len(self.webrtc_channels)) > 0

    def set_status(self, status, detail=""):
        if self.status_callback:
            self.status_callback(status, detail)

    def _update_session_status(self):
        p2p_count = len(self.p2p_ws_clients) + len(self.webrtc_channels)
        if p2p_count > 0:
            self.set_status("busy", f"Direct P2P session active ({p2p_count} viewer{'s' if p2p_count != 1 else ''} – 0 KB/s on VPS)")
        elif self.relay_viewers > 0:
            self.set_status("busy", f"Remote session active ({self.relay_viewers} viewer{'s' if self.relay_viewers != 1 else ''})")
        else:
            self.set_status("online", f"Ready for connections (ID: {self.my_id})")

    def _ensure_background_tasks(self):
        if self.total_viewers > 0:
            if self.stream_task is None or self.stream_task.done():
                self.stream_task = asyncio.create_task(self._stream_frames())
            if self.clip_task is None or self.clip_task.done():
                self.clip_task = asyncio.create_task(self._sync_clipboard())
        else:
            if self.stream_task and not self.stream_task.done():
                self.stream_task.cancel()
            if self.clip_task and not self.clip_task.done():
                self.clip_task.cancel()

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_thread, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.current_ws:
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
                logging.warning(f"Disconnected from signaling server: {e}")
                if not self.has_p2p_viewers:
                    self.set_status("disconnected", str(e))
                time.sleep(2)

    async def _start_p2p_server(self):
        """Starts a local direct WebSocket P2P listener so LAN/VPN/direct peers bypass the VPS completely."""
        if self.p2p_server is not None:
            return
        for port in range(8766, 8776):
            try:
                self.p2p_server = await websockets.serve(
                    self._handle_direct_p2p_client,
                    "0.0.0.0",
                    port,
                    max_size=20 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=40
                )
                self.p2p_port = port
                logging.info(f"Direct P2P listener active on 0.0.0.0:{port}")
                return
            except Exception:
                continue

    def _get_p2p_endpoints(self):
        if not self.p2p_port:
            return []
        return [f"ws://{ip}:{self.p2p_port}" for ip in get_local_ipv4_addresses()]

    def _build_screen_info_payload(self):
        return {
            "type": "screen_info",
            "width": self.capturer.width,
            "height": self.capturer.height,
            "p2p_endpoints": self._get_p2p_endpoints(),
            "p2p_token": self.p2p_token,
            "webrtc_supported": HAS_WEBRTC
        }

    async def _handle_direct_p2p_client(self, ws):
        """Handles a viewer that connected directly to the host's P2P WebSocket endpoint."""
        authenticated = False
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            data = json.loads(raw)
            if data.get("action") != "p2p_auth" or data.get("token") != self.p2p_token:
                await ws.send(json.dumps({"status": "error", "message": "Invalid P2P session token"}))
                return

            authenticated = True
            self.p2p_ws_clients.add(ws)
            # If the viewer was previously counted on the VPS relay, transition it to P2P
            if self.relay_viewers > 0:
                self.relay_viewers = max(0, self.relay_viewers - 1)
            self.force_frame = True
            logging.info("Viewer upgraded to Direct P2P WebSocket (0 KB/s on VPS)!")
            self._update_session_status()
            self._ensure_background_tasks()

            await ws.send(json.dumps({"status": "p2p_connected"}))
            await ws.send(json.dumps(self._build_screen_info_payload()))

            async for message in ws:
                if isinstance(message, str):
                    await self._process_control_message(message)
        except Exception as e:
            logging.debug(f"Direct P2P client closed: {e}")
        finally:
            if authenticated and ws in self.p2p_ws_clients:
                self.p2p_ws_clients.remove(ws)
                logging.info("Direct P2P viewer disconnected.")
                self._update_session_status()
                self._ensure_background_tasks()

    async def _handle_webrtc_offer(self, data, signaling_ws):
        """Handles a WebRTC SDP offer via STUN UDP hole punching so Host <-> Viewer connect directly P2P."""
        if not HAS_WEBRTC:
            return
        try:
            config = RTCConfiguration(iceServers=[
                RTCIceServer(urls=["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"])
            ])
            pc = RTCPeerConnection(configuration=config)
            self.webrtc_pcs.add(pc)
            pc_channels = []

            async def _cleanup_pc():
                for ch in list(pc_channels):
                    self.webrtc_channels.discard(ch)
                self.webrtc_pcs.discard(pc)
                self._update_session_status()
                self._ensure_background_tasks()
                try:
                    await pc.close()
                except Exception:
                    pass

            @pc.on("datachannel")
            def on_datachannel(channel):
                logging.info(f"WebRTC P2P DataChannel '{channel.label}' established (0 KB/s on VPS)!")
                pc_channels.append(channel)
                self.webrtc_channels.add(channel)
                if self.relay_viewers > 0:
                    self.relay_viewers = max(0, self.relay_viewers - 1)
                self.force_frame = True
                self._update_session_status()
                self._ensure_background_tasks()

                try:
                    channel.send(json.dumps(self._build_screen_info_payload()))
                except Exception:
                    pass

                @channel.on("message")
                def on_message(message):
                    if isinstance(message, str):
                        asyncio.ensure_future(self._process_control_message(message))

                @channel.on("close")
                def on_close():
                    self.webrtc_channels.discard(channel)
                    self._update_session_status()
                    self._ensure_background_tasks()

            @pc.on("connectionstatechange")
            async def on_state_change():
                if pc.connectionState in ("failed", "closed", "disconnected"):
                    await _cleanup_pc()

            @pc.on("iceconnectionstatechange")
            async def on_ice_state_change():
                if pc.iceConnectionState in ("failed", "closed", "disconnected"):
                    await _cleanup_pc()

            offer = RTCSessionDescription(sdp=data["sdp"], type=data.get("sdpType", "offer"))
            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            await signaling_ws.send(json.dumps({
                "type": "webrtc_answer",
                "sdp": pc.localDescription.sdp,
                "sdpType": pc.localDescription.type
            }))
        except Exception as e:
            logging.warning(f"WebRTC P2P negotiation failed (staying on fallback): {e}")

    def _send_webrtc_frame(self, channel, frame_bytes):
        """Sends a binary JPEG frame over WebRTC DataChannel, splitting into 48KB chunks if needed."""
        if getattr(channel, "readyState", "") != "open":
            self.webrtc_channels.discard(channel)
            self._update_session_status()
            return
        if getattr(channel, "bufferedAmount", 0) > 256 * 1024:
            # Drop frame if UDP buffer is backed up to prevent latency spikes
            return

        max_chunk = 48000
        if len(frame_bytes) <= max_chunk:
            channel.send(frame_bytes)
            return

        self.frame_seq = (self.frame_seq + 1) & 0xFFFF
        total = (len(frame_bytes) + max_chunk - 1) // max_chunk
        for idx in range(total):
            chunk = frame_bytes[idx * max_chunk:(idx + 1) * max_chunk]
            header = b"DC" + struct.pack(">HBB", self.frame_seq, idx, total)
            channel.send(header + chunk)

    async def _stream_frames(self):
        interval = 1.0 / max(1, self.max_fps)
        consecutive_errors = 0
        while self.running and self.total_viewers > 0:
            start_t = time.time()
            try:
                force = self.force_frame
                self.force_frame = False
                frame_bytes = self.capturer.capture_frame(draw_cursor=False, force=force)
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                logging.debug(f"Capture error (transient): {e}")
                frame_bytes = None
                if consecutive_errors > 30:
                    break
                await asyncio.sleep(0.1)
                continue

            if frame_bytes is not None:
                # 1. Send to Direct P2P WebSocket viewers
                for p2p_ws in list(self.p2p_ws_clients):
                    try:
                        await asyncio.wait_for(p2p_ws.send(frame_bytes), timeout=2.0)
                    except Exception:
                        self.p2p_ws_clients.discard(p2p_ws)
                        self._update_session_status()
                        try:
                            asyncio.create_task(p2p_ws.close())
                        except Exception:
                            pass

                # 2. Send to Direct WebRTC P2P DataChannel viewers
                for channel in list(self.webrtc_channels):
                    try:
                        self._send_webrtc_frame(channel, frame_bytes)
                    except Exception:
                        self.webrtc_channels.discard(channel)
                        self._update_session_status()

                # 3. Send to VPS relay if there is any viewer that hasn't upgraded to P2P yet
                if self.relay_viewers > 0 and self.current_ws:
                    try:
                        await self.current_ws.send(frame_bytes)
                    except Exception as e:
                        logging.debug(f"Relay stream network error: {e}")
                        break

            elapsed = time.time() - start_t
            sleep_t = interval - elapsed
            if sleep_t > 0:
                await asyncio.sleep(sleep_t)
            else:
                await asyncio.sleep(0.001)

    async def _sync_clipboard(self):
        while self.running and self.total_viewers > 0:
            try:
                cur = get_clipboard_text()
                if cur and cur != self.last_clipboard and len(cur) < 500000:
                    self.last_clipboard = cur
                    payload = json.dumps({"type": "clipboard", "text": cur})
                    for p2p_ws in list(self.p2p_ws_clients):
                        try:
                            await p2p_ws.send(payload)
                        except Exception:
                            pass
                    for channel in list(self.webrtc_channels):
                        try:
                            if getattr(channel, "readyState", "") == "open":
                                channel.send(payload)
                        except Exception:
                            pass
                    if self.relay_viewers > 0 and self.current_ws:
                        await self.current_ws.send(payload)
            except Exception:
                pass
            await asyncio.sleep(0.8)

    async def _process_control_message(self, message, signaling_ws=None):
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "viewer_connected":
                self.relay_viewers += 1
                self.force_frame = True
                logging.info(f"Viewer connected via signaling! Offering Direct P2P upgrade...")
                self._update_session_status()
                if signaling_ws:
                    await signaling_ws.send(json.dumps(self._build_screen_info_payload()))
                self._ensure_background_tasks()

            elif msg_type == "p2p_takeover":
                # Viewer confirmed it switched to P2P
                self._update_session_status()

            elif msg_type == "webrtc_offer" and signaling_ws:
                asyncio.create_task(self._handle_webrtc_offer(data, signaling_ws))

            elif msg_type == "viewer_disconnected":
                remaining = data.get("remaining_viewers")
                if remaining == 0:
                    # All signaling viewers have closed their sessions; clean up any lingering P2P/WebRTC state
                    self.relay_viewers = 0
                    for p2p_ws in list(self.p2p_ws_clients):
                        self.p2p_ws_clients.discard(p2p_ws)
                        try:
                            asyncio.create_task(p2p_ws.close())
                        except Exception:
                            pass
                    for ch in list(self.webrtc_channels):
                        self.webrtc_channels.discard(ch)
                        try:
                            ch.close()
                        except Exception:
                            pass
                    for pc in list(self.webrtc_pcs):
                        self.webrtc_pcs.discard(pc)
                        try:
                            asyncio.create_task(pc.close())
                        except Exception:
                            pass
                else:
                    if self.relay_viewers > 0:
                        self.relay_viewers = max(0, self.relay_viewers - 1)
                    for ch in list(self.webrtc_channels):
                        if getattr(ch, "readyState", "") != "open":
                            self.webrtc_channels.discard(ch)

                logging.info(f"Signaling viewer closed. Active P2P viewers: {len(self.p2p_ws_clients) + len(self.webrtc_channels)}, Relay viewers: {self.relay_viewers}")
                self._update_session_status()
                self._ensure_background_tasks()

            elif msg_type == "clipboard":
                text = data.get("text", "")
                if text and text != self.last_clipboard:
                    self.last_clipboard = text
                    set_clipboard_text(text)
                    logging.info("Clipboard received from viewer.")

            elif msg_type == "input":
                evt_data = data.get("data", {})
                self.input_controller.handle_event(evt_data)
                if evt_data.get("event") != "mouse_move":
                    self.force_frame = True
                self._ensure_background_tasks()

            elif msg_type == "set_quality":
                self.capturer.update_settings(
                    quality=data.get("quality"),
                    scale=data.get("scale")
                )
                self.force_frame = True
                logging.info(f"Quality profile updated: scale={self.capturer.scale}, q={self.capturer.quality}")
        except Exception as e:
            logging.warning(f"Error processing control input: {e}")

    async def _connect_and_serve(self):
        await self._start_p2p_server()
        self.set_status("connecting", "Connecting to signaling server...")
        async with websockets.connect(
            self.relay_url,
            max_size=20 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=40
        ) as ws:
            self.current_ws = ws
            reg_payload = {
                "action": "register",
                "id": self.my_id,
                "password": self.my_password,
                "p2p_endpoints": self._get_p2p_endpoints(),
                "p2p_token": self.p2p_token
            }
            await ws.send(json.dumps(reg_payload))

            resp_str = await ws.recv()
            resp = json.loads(resp_str)
            if resp.get("status") != "registered":
                self.set_status("error", resp.get("message", "Registration failed"))
                return

            logging.info(f"Successfully registered as host '{self.my_id}' (Direct P2P endpoints: {self._get_p2p_endpoints()})!")
            self._update_session_status()

            async for message in ws:
                if isinstance(message, str):
                    await self._process_control_message(message, signaling_ws=ws)
