import asyncio
import json
import logging
import os
import pathlib
import websockets
from websockets.http11 import Response
from websockets.datastructures import Headers

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

HOST_PORT = int(os.environ.get("PORT", 8765))
WEB_FILE = pathlib.Path(__file__).parent / "web" / "index.html"

# Active hosts: peer_id -> websocket
hosts = {}
# Active viewers: peer_id -> list of viewer websockets
viewers = {}
# Passwords: peer_id -> password
peer_auth = {}

def process_http_request(connection, request):
    """Serves the Web Viewer HTML if opened via standard HTTP browser."""
    # Check if this is a WebSocket upgrade handshake
    upgrade = request.headers.get("Upgrade", "")
    if upgrade and upgrade.lower() == "websocket":
        return None  # Proceed to WebSocket handshake!

    path = request.path
    if path == "/" or path.startswith("/?") or path == "/index.html":
        if WEB_FILE.exists():
            with open(WEB_FILE, "rb") as f:
                body = f.read()
            return Response(
                status_code=200,
                reason_phrase="OK",
                headers=Headers([
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Length", str(len(body)))
                ]),
                body=body
            )
        else:
            body = b"<h1>RustDesk Light Relay Server is Running</h1>"
            return Response(
                status_code=200,
                reason_phrase="OK",
                headers=Headers([
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Length", str(len(body)))
                ]),
                body=body
            )
    if path.startswith("/download/"):
        filename = os.path.basename(path.split("?")[0])
        file_path = pathlib.Path(__file__).parent / "web" / "download" / filename
        if file_path.exists() and file_path.is_file():
            with open(file_path, "rb") as f:
                body = f.read()
            mime = "application/zip" if filename.endswith(".zip") else "application/octet-stream"
            return Response(
                status_code=200,
                reason_phrase="OK",
                headers=Headers([
                    ("Content-Type", mime),
                    ("Content-Disposition", f'attachment; filename="{filename}"'),
                    ("Content-Length", str(len(body)))
                ]),
                body=body
            )
        else:
            return Response(
                status_code=404,
                reason_phrase="Not Found",
                headers=Headers([("Content-Type", "text/plain")]),
                body=b"Datei nicht gefunden"
            )

    return None # Proceed to WebSocket upgrade

async def handle_websocket(websocket):
    peer_id = None
    role = None
    try:
        # First message must be registration or connection request
        first_msg = await websocket.recv()
        logging.info(f"[WS-IN] Erste Nachricht: {first_msg}")
        data = json.loads(first_msg)
        action = data.get("action")
        raw_id = str(data.get("id", "")).replace(" ", "").strip()
        pwd = data.get("password", "").strip()

        if action == "register":
            # HOST registration
            role = "host"
            peer_id = raw_id
            if not peer_id:
                await websocket.send(json.dumps({"status": "error", "message": "ID erforderlich"}))
                return

            hosts[peer_id] = websocket
            peer_auth[peer_id] = pwd
            logging.info(f"[+] Host registriert: ID '{peer_id}'")
            await websocket.send(json.dumps({"status": "registered", "id": peer_id}))

            async for message in websocket:
                # Forward frames/messages from host to all connected viewers of this host
                if peer_id in viewers:
                    dead_viewers = []
                    for v_ws in viewers[peer_id]:
                        try:
                            await v_ws.send(message)
                        except Exception:
                            dead_viewers.append(v_ws)
                    for dead in dead_viewers:
                        if dead in viewers[peer_id]:
                            viewers[peer_id].remove(dead)

        elif action == "connect":
            # VIEWER connecting to host
            role = "viewer"
            target_id = str(data.get("target_id", "")).replace(" ", "").strip()
            logging.info(f"[?] Viewer will verbinden mit '{target_id}'. Registrierte Hosts: {list(hosts.keys())}")

            if target_id not in hosts:
                logging.info(f"[-] Host '{target_id}' nicht online")
                await websocket.send(json.dumps({"status": "error", "message": f"Host '{target_id}' ist nicht online."}))
                return

            expected_pwd = peer_auth.get(target_id, "")
            if expected_pwd and pwd != expected_pwd:
                logging.info(f"[-] Falsches Passwort für Host '{target_id}' (erwartet: '{expected_pwd}', erhalten: '{pwd}')")
                await websocket.send(json.dumps({"status": "error", "message": "Falsches Passwort."}))
                return

            if target_id not in viewers:
                viewers[target_id] = []
            viewers[target_id].append(websocket)
            host_ws = hosts[target_id]

            logging.info(f"[+] Viewer verbunden mit Host: ID '{target_id}'")
            await websocket.send(json.dumps({"status": "connected", "target_id": target_id}))
            
            # Notify host that viewer joined
            try:
                await host_ws.send(json.dumps({"type": "viewer_connected"}))
            except Exception:
                pass

            async for message in websocket:
                # Forward control inputs (mouse/keyboard) from viewer to host
                if target_id in hosts:
                    try:
                        await hosts[target_id].send(message)
                    except Exception:
                        break

    except websockets.exceptions.ConnectionClosed as e:
        logging.info(f"[WS-CLOSED] Role '{role}', Peer '{peer_id}': code={e.code}, reason={e.reason}")
    except Exception as e:
        logging.exception(f"[WS-ERR] Fehler bei Peer '{peer_id}': {e}")
    finally:
        if role == "host" and peer_id in hosts:
            if hosts[peer_id] == websocket:
                del hosts[peer_id]
                peer_auth.pop(peer_id, None)
                logging.info(f"[-] Host getrennt: ID '{peer_id}'")
                if peer_id in viewers:
                    for v_ws in list(viewers[peer_id]):
                        try:
                            await v_ws.send(json.dumps({"type": "host_disconnected"}))
                        except Exception:
                            pass
        elif role == "viewer":
            for t_id, v_list in list(viewers.items()):
                if websocket in v_list:
                    v_list.remove(websocket)
                    logging.info(f"[-] Viewer getrennt von ID '{t_id}'")
                    if t_id in hosts:
                        try:
                            await hosts[t_id].send(json.dumps({"type": "viewer_disconnected"}))
                        except Exception:
                            pass

async def main():
    logging.info(f"Relay & Handshake Server startet auf 0.0.0.0:{HOST_PORT}...")
    async with websockets.serve(
        handle_websocket,
        "0.0.0.0",
        HOST_PORT,
        process_request=process_http_request,
        max_size=25 * 1024 * 1024, # 25MB max frame size
        ping_interval=20,
        ping_timeout=40
    ):
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Server beendet.")
