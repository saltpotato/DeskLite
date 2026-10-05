# ⚡ RustDesk Light - Remote Desktop

Ein leichtgewichtiges, schlankes Remote-Desktop-System (wie RustDesk Light), das Firewalls und Router-NATs ohne Portfreigaben auf den Zielrechnern umgeht.

---

## 🛠️ Funktionsweise & Firewall-Bypass (Architektur)

```
[ Host PC (zuhause/Büro) ]                [ Viewer PC / Handy / Browser ]
           │                                             │
           │ (Ausgehende WebSocket-Verbindung)           │ (Ausgehende WebSocket-Verbindung)
           ▼                                             ▼
     ┌─────────────────────────────────────────────────────────┐
     │           VPS Relay & Handshake Server (Docker)         │
     │                 Port: 8765 (TCP / WebSocket)            │
     └─────────────────────────────────────────────────────────┘
```

- **Keine Portweiterleitung am Router nötig:** Sowohl der Host (der gesteuert wird) als auch der Viewer (der steuert) verbinden sich **ausgehend** (Outbound) zum Relay-Server auf deinem VPS. Ausgehende TCP-Verbindungen werden von praktisch jeder normalen Firewall, FritzBox und jedem Firmennetzwerk standardmäßig erlaubt.
- **Relay / Vermittlung:** Sobald der Viewer die Kennung (Host-ID) eingibt, vermittelt der Server die Bild- und Eingabedaten in Echtzeit per WebSocket.
- **Natives Desktop GUI & Web-Browser Client:** Du kannst entweder die Desktop-App auf beiden PCs starten ODER dich einfach über jeden Browser (z. B. auf Handy, Tablet, Laptop) auf `http://<dein-vps>:8765` einloggen!

---

## 🚀 1. Handshake & Relay Server auf VPS starten (Docker)

Kopiere den Ordner `server/` auf deinen VPS oder klone ihn dort hin.

### Mit Docker Compose (empfohlen):
```bash
cd server
docker compose up -d --build
```

Der Server lauscht auf Port `8765`. 
*(Optional: Du kannst auf deinem VPS auch Nginx/Caddy davor schalten mit SSL für `wss://deine-domain.de`).*

---

## 💻 2. Client-App auf den PCs starten

### Voraussetzungen:
- Python 3.10+
- Abhängigkeiten installieren (einmalig):
  ```bash
  pip install -r requirements.txt
  ```

### Starten:
- Unter Windows einfach doppelklicken auf `start_app.bat` oder im Terminal:
  ```bash
  python main.py
  ```

---

## 🎮 3. Bedienung

1. **Relay-Server-Adresse eintragen:**
   - Trage unten im Feld *Relay-Server* die Adresse deines VPS ein:  
     `ws://DEINE_VPS_IP:8765` (oder `wss://deine-domain.de`, falls mit SSL).
   - Klicke auf **Server ändern**. Der PC registriert sich sofort beim Relay.
2. **PC freigeben (Host):**
   - Auf der linken Seite siehst du deine **ID** (z. B. `384 192`) und das **Passwort**.
   - Teile diese ID demjenigen mit, der dich fernsteuern soll.
3. **Anderen PC fernsteuern (Viewer):**
   - Auf der rechten Seite die **Partner-ID** und optional das Passwort eingeben.
   - Auf **🚀 Verbinden** klicken.
   - Ein neues Fenster öffnet sich mit dem Desktop des Partners in Echtzeit!

### Tastenkombinationen im Viewer-Fenster:
- **Mausklicks & Scrollen:** 1:1 wie vor Ort.
- **Tastatur:** Alle Tasten werden direkt an den entfernten PC weitergeleitet.
- **⛶ Vollbild:** Klick auf den Toolbar-Button oder Taste `F11`.
- **Ctrl+Alt+Entf:** Button in der oberen Menüleiste.

---

## 🌐 4. Bonus: Browser-Zugriff ohne Installation

Auf deinem VPS ist im Relay-Server direkt ein HTML5-Viewer integriert:
- Öffne einfach im Browser:
  ```
  http://DEINE_VPS_IP:8765
  ```
- Gib die Host-ID ein und steuere den Ziel-PC direkt im Browserfenster!
