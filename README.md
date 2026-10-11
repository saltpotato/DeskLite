# ⚡ DeskLite - Remote Desktop

Ein schlankes, leichtgewichtiges Remote-Desktop-System in Python – mit Desktop-App und direktem HTML5-Web-Viewer im Browser.

---

## ✨ Features

- **Desktop-App & HTML5-Web-Viewer:** Entweder über die schlanke Python/Windows-App nutzen oder direkt ohne Installation im Browser (`http://<dein-vps>:8765`) fernsteuern.
- **Native 1:1 Darstellung & Qualitätsprofile:** Drei umschaltbare Profile (**🚀 Schnell**, **⚖️ Normal**, **💎 HQ**) mit gestochen scharfer 1:1-Auflösung und OpenCV-gestützter Differenzbild-Erkennung für niedrige Bandbreite.
- **Immersiver Vollbildmodus:** Echtes Vollbild im Browser und Desktop-Viewer mit automatisch ausblendender Floating-Toolbar am oberen Bildschirmrand.
- **Zwischenablage-Synchronisation (Clipboard):** Kopierter Text wird zwischen Host und Viewer/Browser übertragen (`Strg+C` / `Strg+V` bzw. **📋 Einfügen**-Button).
- **Quick-Shortcuts in der Toolbar:** Schnellzugriff auf **🖥️ Desktop (Win+D)**, **🪟 Startmenü**, **📁 Explorer** und **⚡ Task-Manager**.
- **Partner-Historie:** Zuletzt verbundene Partner-IDs werden lokal gespeichert und können mit einem Klick wiederverbunden werden.
- **Admin- & UAC-Unterstützung (Windows):** Läuft ohne Konsolenfenster im Hintergrund und unterstützt die Steuerung von erhöhten Fenstern sowie UAC-Abfragen.
- **Auto-Reconnect:** Automatische Wiederverbindung bei kurzen Netzwerkunterbrechungen.
- **Standalone `.exe` Generator:** Über `build.ps1` lässt sich eine eigenständige `DeskLite.exe` bauen (keine Python-Installation auf dem Zielgerät nötig), die auch direkt über das Web-Interface heruntergeladen werden kann.

---

## 🛠️ Architektur

```
[ Host PC ]                               [ Viewer PC / Browser ]
     │                                               │
     │ (WebSocket-Verbindung)                        │ (WebSocket-Verbindung)
     ▼                                               ▼
┌─────────────────────────────────────────────────────────┐
│             DeskLite Relay Server (Docker)              │
│                 Port: 8765 (TCP / WebSocket)            │
└─────────────────────────────────────────────────────────┘
```

---

## 🚀 1. Relay-Server starten (Docker)

Kopiere den Ordner `server/` auf deinen Server oder klone das Repository dort hin:

```bash
cd server
docker compose up -d --build
```

Der Server stellt auf Port `8765` sowohl den WebSocket-Relay als auch das HTML5-Web-Interface bereit.  
*(Optional kann ein Reverse-Proxy wie Nginx oder Caddy mit HTTPS/WSS vorgeschaltet werden).*

---

## 💻 2. Client starten oder `.exe` bauen

### Direkt mit Python starten
1. **Voraussetzungen:** Python 3.10+
2. **Abhängigkeiten installieren (einmalig):**
   ```bash
   pip install -r requirements.txt
   ```
3. **Starten:**
   - Unter Windows per Doppelklick auf `start_app.bat` (startet ohne Konsolenfenster) oder:
   ```bash
   pythonw main.py
   ```

### Eigenständige `DeskLite.exe` erstellen (optional)
Mit dem mitgelieferten PowerShell-Skript wird eine portable Einzeldatei (`DeskLite.exe`) gebaut, in der die Server-Adresse bereits hinterlegt ist:
```powershell
.\build.ps1 -ServerUrl "ws://DEINE_SERVER_IP:8765"
```
Die erzeugte Datei liegt anschließend unter `server/web/download/DeskLite.exe` und kann auf das Web-Interface hochgeladen werden.

---

## 🎮 3. Bedienung

1. **Relay-Server einstellen:**
   - Unten im Feld *Relay-Server* die Adresse eintragen (`ws://DEINE_SERVER_IP:8765` oder `wss://deine-domain.de`) und auf **Speichern** klicken.
2. **Diesen PC freigeben (Host):**
   - Links werden deine **6-stellige ID** (z. B. `384 192`) und das **Einmalkennwort** angezeigt.
   - Über **🔄 Neu** lassen sich jederzeit eine neue ID oder ein neues Passwort generieren.
   - Über den UAC-Button unten lässt sich bei Bedarf die Windows-Bildschirmabdunkelung bei UAC-Dialogen umschalten, damit Abfragen im Stream sichtbar und klickbar bleiben.
3. **Partner steuern (Viewer / Browser):**
   - In der Desktop-App rechts oder im Browser unter `http://DEINE_SERVER_IP:8765` die **Partner-ID** und das Passwort eingeben und auf **Verbinden** klicken.
   - Im Vollbildmodus (`⛶ Vollbild`) blendet sich die obere Leiste automatisch aus und klappt wieder herunter, sobald man mit der Maus an den oberen Bildschirmrand fährt.

