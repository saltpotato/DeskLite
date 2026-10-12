# ⚡ DeskLite - Lightweight P2P Remote Desktop

A lightweight, high-performance Remote Desktop system built in Python — featuring **Direct Peer-to-Peer (P2P)** streaming, **GPU-direct DXGI Desktop Duplication**, a native desktop app, and a zero-install HTML5 web viewer in the browser.

---

## ✨ Features

- **Direct Peer-to-Peer (P2P) Architecture (`0 KB/s` on VPS):**
  - Your VPS only acts as a lightweight **Signaling Broker** during the initial 0.5-second handshake (ID/password verification & endpoint exchange).
  - **Stage 1 – Direct LAN / VPN Handover:** Automatically switches to a direct local WebSocket (`ws://<host-ip>:8766`) when on the same network or VPN (`< 1 ms` latency).
  - **Stage 2 – WebRTC P2P (`aiortc` + STUN UDP Hole Punching):** Traverses NAT routers across the internet via Google STUN (`stun.l.google.com:19302`) over an encrypted `RTCDataChannel`, seamlessly cutting off VPS relay traffic (`p2p_takeover`). Falls back to the server relay only if strict firewalls block all P2P traffic.
- **GPU-Direct DXGI Desktop Duplication (`dxcam`):**
  - Captures frames directly from GPU VRAM (`IDXGIOutputDuplication`) with `0%` CPU overhead when the screen is idle.
  - Includes automatic interactive user token impersonation (`ImpersonateLoggedOnUser`) so DXGI works even inside a `SYSTEM` process, plus instant GDI (`mss`) fallback when Windows switches to the UAC Secure Desktop (`Winlogon`).
- **Desktop App & HTML5 Web Viewer:** Control remote machines using the lightweight Python/Windows desktop app or directly from any browser (`http://<your-server>:8765`).
- **Native 1:1 Sharpness & Quality Profiles:** Switch live between three profiles (**🚀 Fast**, **⚖️ Balanced**, **💎 HQ**) with crisp 1:1 native resolution.
- **Immersive Fullscreen Mode:** True `100vw × 100vh` fullscreen in both browser and desktop viewer with an auto-hiding floating top toolbar that slides down on hover.
- **Clipboard Synchronization:** Bi-directional text clipboard sync between host and viewer/browser (`Ctrl+C` / `Ctrl+V` or the **📋 Paste** toolbar button).
- **Quick Toolbar Shortcuts:** One-click shortcuts for **🖥️ Show Desktop (`Win+D`)**, **🪟 Start Menu**, **📁 File Explorer**, and **⚡ Task Manager**.
- **Recent Partners History:** Automatically remembers recently connected Partner IDs for one-click reconnection.
- **Admin, `SYSTEM` & UAC Support (Windows):** Launches windowless in the background (`pythonw.exe`), elevates to `NT AUTHORITY\SYSTEM` in the interactive session, and allows viewing and clicking elevated windows and UAC prompts.
- **Standalone `.exe` Builder:** Includes `build.ps1` to package a standalone `DeskLite.exe` binary (no Python installation required on target PCs) with a direct download button in the web interface.

---

## 🛠️ Architecture

```
[ Host PC (DXGI GPU Capture) ]  ◄══════ Direct P2P Stream (0 KB/s on VPS) ══════►  [ Viewer PC / Browser ]
             │                    (1. Direct LAN/VPN WS  |  2. WebRTC STUN UDP)               │
             │                                                                                │
             └───────────────►  ┌──────────────────────────────────────────┐  ◄───────────────┘
                1. Handshake    │     DeskLite Signaling Server (VPS)      │    1. Handshake
                   Only         │      Port: 8765 (Auth + ICE Broker)      │       Only
                                └──────────────────────────────────────────┘
```

---

## 🚀 1. Start the Signaling & Web Server (Docker)

Copy the `server/` directory to your VPS or clone the repository there:

```bash
cd server
docker compose up -d --build
```

The server listens on port `8765`, serves the HTML5 Web Viewer, and brokers P2P connections between hosts and viewers (acting as a fallback relay only if direct P2P is blocked by a strict firewall).  
*(Optional: Put a reverse proxy like Nginx or Caddy in front of it for HTTPS / `wss://`.)*

---

## 💻 2. Run the Client or Build `DeskLite.exe`

### Run directly with Python
1. **Prerequisites:** Python 3.10+
2. **Install dependencies (once):**
   ```bash
   pip install -r requirements.txt
   ```
3. **Start:**
   - On Windows, double-click `start_app.bat` (launches windowless via `pythonw.exe`) or run:
   ```bash
   pythonw main.py
   ```

### Build a standalone `DeskLite.exe` (optional)
Use the included PowerShell script to build a single-file portable `DeskLite.exe` with your server URL pre-configured:
```powershell
.\build.ps1
```
The compiled binary is placed in `server/web/download/DeskLite.exe` so it can be downloaded directly from the web viewer.

---

## 🎮 3. Usage

1. **Configure Signaling Server:**
   - Enter your server address in the *Relay Server* field at the bottom (`ws://YOUR_SERVER_IP:8765` or `wss://your-domain.com`) and click **Save**.
2. **Share This PC (Host):**
   - Your **9-digit ID** (e.g. `384 192 741`) and **One-Time Password** are displayed on the left card.
   - Click **🔄 New** at any time to generate a new ID or password.
   - Use the UAC button at the bottom to toggle UAC prompt visibility on the normal desktop while keeping UAC confirmation prompts active (`ConsentPromptBehaviorAdmin = 5`).
3. **Control a Remote PC (Viewer / Browser):**
   - Enter the **Partner ID** and password on the right card of the desktop app (or in your browser at `http://YOUR_SERVER_IP:8765`) and click **Connect**.
   - After the initial handshake, the status bar automatically switches to **`🟢 Direct P2P`** or **`🟢 Direct P2P WebRTC`** (`0 KB/s on VPS`).
   - In fullscreen mode (`⛶ Fullscreen`), the top bar hides automatically and reappears when moving the cursor to the top edge of the screen.
