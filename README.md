# ⚡ DeskLite - Remote Desktop

A lightweight, fast Remote Desktop system built in Python — featuring a native desktop app and a zero-install HTML5 web viewer in the browser.

---

## ✨ Features

- **Desktop App & HTML5 Web Viewer:** Control remote machines using the lightweight Python/Windows desktop app or directly from any browser (`http://<your-server>:8765`).
- **Native 1:1 Sharpness & Quality Profiles:** Switch live between three profiles (**🚀 Fast**, **⚖️ Balanced**, **💎 HQ**) with crisp 1:1 native resolution and OpenCV-powered frame-diffing for low bandwidth usage.
- **Immersive Fullscreen Mode:** True `100vw × 100vh` fullscreen in both browser and desktop viewer with an auto-hiding floating top toolbar that slides down on hover.
- **Clipboard Synchronization:** Bi-directional text clipboard sync between host and viewer/browser (`Ctrl+C` / `Ctrl+V` or the **📋 Paste** toolbar button).
- **Quick Toolbar Shortcuts:** One-click shortcuts for **🖥️ Show Desktop (`Win+D`)**, **🪟 Start Menu**, **📁 File Explorer**, and **⚡ Task Manager**.
- **Recent Partners History:** Automatically remembers recently connected Partner IDs for one-click reconnection.
- **Admin & UAC Support (Windows):** Runs windowless in the background (`pythonw.exe`) and supports interacting with elevated windows and UAC prompts.
- **Auto-Reconnect:** Automatically reconnects on brief network interruptions.
- **Standalone `.exe` Builder:** Includes `build.ps1` to package a standalone `DeskLite.exe` binary (no Python installation required on target PCs) with a direct download button in the web interface.

---

## 🛠️ Architecture

```
[ Host PC ]                               [ Viewer PC / Browser ]
     │                                               │
     │ (WebSocket Connection)                        │ (WebSocket Connection)
     ▼                                               ▼
┌─────────────────────────────────────────────────────────┐
│             DeskLite Relay Server (Docker)              │
│                 Port: 8765 (TCP / WebSocket)            │
└─────────────────────────────────────────────────────────┘
```

---

## 🚀 1. Start the Relay Server (Docker)

Copy the `server/` directory to your server or clone the repository there:

```bash
cd server
docker compose up -d --build
```

The server listens on port `8765` and serves both the WebSocket relay and the HTML5 Web Viewer.  
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
   - On Windows, double-click `start_app.bat` (launches without a console window) or run:
   ```bash
   pythonw main.py
   ```

### Build a standalone `DeskLite.exe` (optional)
Use the included PowerShell script to build a single-file portable `DeskLite.exe` with your relay server URL pre-configured:
```powershell
.\build.ps1
```
The compiled binary is placed in `server/web/download/DeskLite.exe` so it can be downloaded directly from the web viewer.

---

## 🎮 3. Usage

1. **Configure Relay Server:**
   - Enter your server address in the *Relay Server* field at the bottom (`ws://YOUR_SERVER_IP:8765` or `wss://your-domain.com`) and click **Save**.
2. **Share This PC (Host):**
   - Your **6-digit ID** (e.g. `384 192`) and **One-Time Password** are displayed on the left card.
   - Click **🔄 New** at any time to generate a new ID or password.
   - Use the UAC button at the bottom to toggle Windows Secure Desktop dimming so UAC prompts remain visible and clickable in the remote stream.
3. **Control a Remote PC (Viewer / Browser):**
   - Enter the **Partner ID** and password on the right card of the desktop app (or in your browser at `http://YOUR_SERVER_IP:8765`) and click **Connect**.
   - In fullscreen mode (`⛶ Fullscreen`), the top bar hides automatically and reappears when moving the cursor to the top edge of the screen.


