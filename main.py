import tkinter as tk
from tkinter import ttk, messagebox
import common
from host_service import HostService
from viewer_window import RemoteDesktopViewer

class DeskLiteApp:
    def __init__(self, root):
        self.root = root
        is_adm = common.is_admin()
        self.root.title(f"DeskLite {'[Administrator]' if is_adm else ''} - Remote Desktop")
        self.root.resizable(False, False)
        
        self.config = common.load_config()
        self.host_service = None

        self._apply_styles()
        self._build_ui()
        
        # Start Host Service
        self.start_host_service()
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _apply_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        
        # Custom colors
        self.bg_color = "#f4f6f9"
        self.card_bg = "#ffffff"
        self.primary_color = "#1976d2"
        self.text_color = "#2c3e50"
        
        self.root.configure(bg=self.bg_color)
        
        style.configure("TLabel", background=self.bg_color, foreground=self.text_color, font=("Segoe UI", 10))
        style.configure("Card.TFrame", background=self.card_bg, relief="solid", borderwidth=1)
        style.configure("CardLabel.TLabel", background=self.card_bg, foreground=self.text_color, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=self.card_bg, foreground="#1565c0", font=("Segoe UI", 12, "bold"))
        style.configure("Header.TLabel", background=self.bg_color, foreground="#0d47a1", font=("Segoe UI", 15, "bold"))
        style.configure("ID.TLabel", background=self.card_bg, foreground="#2e7d32", font=("Consolas", 18, "bold"))
        style.configure("PW.TLabel", background=self.card_bg, foreground="#d84315", font=("Consolas", 14, "bold"))
        style.configure("Status.TLabel", background=self.bg_color, font=("Segoe UI", 9))
        
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), background=self.primary_color, foreground="white")
        style.map("Primary.TButton", background=[("active", "#1565c0")])

    def _build_ui(self):
        # Header banner
        header_text = "⚡ DeskLite"
        if common.is_admin():
            header_text += " [Administrator]"
        header = ttk.Label(self.root, text=header_text, style="Header.TLabel")
        header.pack(pady=(16, 12))

        # Main 2-column container
        columns = ttk.Frame(self.root)
        columns.pack(fill=tk.BOTH, expand=True, padx=20)
        
        # LEFT CARD: Dieser PC (Host)
        left_card = ttk.Frame(columns, style="Card.TFrame", padding=16)
        left_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        
        ttk.Label(left_card, text="Dieser PC (Freigabe)", style="Title.TLabel").pack(anchor=tk.W, pady=(0, 12))
        
        ttk.Label(left_card, text="Deine ID:", style="CardLabel.TLabel").pack(anchor=tk.W)
        self.lbl_id = ttk.Label(left_card, text=self.config.get("my_id", "--- ---"), style="ID.TLabel")
        self.lbl_id.pack(anchor=tk.W, pady=(2, 8))
        
        id_btn_frame = ttk.Frame(left_card, style="Card.TFrame")
        id_btn_frame.pack(fill=tk.X, pady=(0, 12))
        btn_copy_id = ttk.Button(id_btn_frame, text="📋 ID kopieren", command=self.copy_id)
        btn_copy_id.pack(side=tk.LEFT)
        btn_new_id = ttk.Button(id_btn_frame, text="🔄 Neu", command=self.new_id)
        btn_new_id.pack(side=tk.LEFT, padx=6)

        ttk.Label(left_card, text="Einmalkennwort:", style="CardLabel.TLabel").pack(anchor=tk.W)
        self.lbl_pw = ttk.Label(left_card, text=self.config.get("my_password", "------"), style="PW.TLabel")
        self.lbl_pw.pack(anchor=tk.W, pady=(2, 8))

        btn_new_pw = ttk.Button(left_card, text="🔄 Neues Passwort", command=self.new_password)
        btn_new_pw.pack(anchor=tk.W)

        # RIGHT CARD: Partner steuern (Viewer)
        right_card = ttk.Frame(columns, style="Card.TFrame", padding=16)
        right_card.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(10, 0))
        
        ttk.Label(right_card, text="Partner steuern", style="Title.TLabel").pack(anchor=tk.W, pady=(0, 12))
        
        ttk.Label(right_card, text="Partner-ID eingeben:", style="CardLabel.TLabel").pack(anchor=tk.W, pady=(0, 4))
        recent_ids = [p["id"] for p in self.config.get("recent_partners", [])]
        self.entry_partner_id = ttk.Combobox(right_card, font=("Consolas", 14), width=17, values=recent_ids)
        self.entry_partner_id.pack(anchor=tk.W, pady=(0, 12))
        self.entry_partner_id.bind("<Return>", lambda e: self.connect_partner())
        self.entry_partner_id.bind("<<ComboboxSelected>>", self.on_partner_selected)

        ttk.Label(right_card, text="Partner-Passwort (falls gesetzt):", style="CardLabel.TLabel").pack(anchor=tk.W, pady=(0, 4))
        self.entry_partner_pw = ttk.Entry(right_card, font=("Segoe UI", 11), width=18, show="*")
        self.entry_partner_pw.pack(anchor=tk.W, pady=(0, 16))
        self.entry_partner_pw.bind("<Return>", lambda e: self.connect_partner())

        # Prefill last partner
        if recent_ids:
            self.entry_partner_id.current(0)
            self.on_partner_selected()

        btn_connect = ttk.Button(right_card, text="🚀 Verbinden", style="Primary.TButton", command=self.connect_partner)
        btn_connect.pack(anchor=tk.W, ipady=4, fill=tk.X)

        # BOTTOM: Server & Status
        bottom_frame = ttk.Frame(self.root, padding=(20, 10))
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM)

        server_row = ttk.Frame(bottom_frame)
        server_row.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(server_row, text="Relay-Server:").pack(side=tk.LEFT)
        self.entry_server = ttk.Entry(server_row, width=22)
        self.entry_server.insert(0, self.config.get("relay_server", "ws://localhost:8765"))
        self.entry_server.pack(side=tk.LEFT, padx=6)

        btn_save_server = ttk.Button(server_row, text="Speichern", command=self.save_server)
        btn_save_server.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(server_row, text="Profil:").pack(side=tk.LEFT)
        self.combo_mode = ttk.Combobox(server_row, values=["🚀 Schnell", "⚖️ Normal", "💎 HQ"], state="readonly", width=10)
        curr_q = self.config.get("quality", 75)
        if curr_q <= 65:
            self.combo_mode.current(0)
        elif curr_q >= 85:
            self.combo_mode.current(2)
        else:
            self.combo_mode.current(1)
        self.combo_mode.pack(side=tk.LEFT, padx=6)
        self.combo_mode.bind("<<ComboboxSelected>>", self.on_mode_change)

        if not common.is_admin():
            btn_admin = ttk.Button(server_row, text="🛡️ Als Admin ausführen", command=common.restart_as_admin)
            btn_admin.pack(side=tk.RIGHT, padx=4)
        else:
            if common.is_service_installed():
                self.btn_service = ttk.Button(server_row, text="🗑️ Dienst entfernen", command=self.toggle_windows_service)
                self.btn_service.pack(side=tk.RIGHT, padx=4)
            else:
                self.btn_service = ttk.Button(server_row, text="⚙️ Als Dienst einrichten", command=self.toggle_windows_service)
                self.btn_service.pack(side=tk.RIGHT, padx=4)

            if common.get_uac_secure_desktop():
                self.btn_uac = ttk.Button(server_row, text="🛡️ UAC-Freigabe", command=self.enable_uac_remote)
                self.btn_uac.pack(side=tk.RIGHT, padx=4)

        # Status text
        self.lbl_status = ttk.Label(bottom_frame, text="🟡 Initialisiere...", style="Status.TLabel")
        self.lbl_status.pack(anchor=tk.W)

    def start_host_service(self):
        if self.host_service:
            self.host_service.stop()
            
        self.host_service = HostService(
            relay_url=self.config.get("relay_server", "ws://localhost:8765"),
            my_id=self.config.get("my_id"),
            my_password=self.config.get("my_password"),
            quality=self.config.get("quality", 75),
            max_fps=self.config.get("max_fps", 30),
            scale=self.config.get("scale", 1.0),
            status_callback=self.on_host_status
        )
        self.host_service.start()

    def on_host_status(self, status, detail=""):
        def update():
            if status == "online":
                self.lbl_status.config(text=f"🟢 {detail}", foreground="#2e7d32")
            elif status == "connecting":
                self.lbl_status.config(text=f"🟡 {detail}", foreground="#f57f17")
            elif status == "busy":
                self.lbl_status.config(text=f"🔵 {detail}", foreground="#1565c0")
            elif status == "error" or status == "disconnected":
                self.lbl_status.config(text=f"🔴 {detail}", foreground="#c62828")
        self.root.after(0, update)

    def copy_id(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.config.get("my_id", ""))
        messagebox.showinfo("Kopiert", "Deine ID wurde in die Zwischenablage kopiert!")

    def new_id(self):
        new_val = common.generate_id()
        self.config["my_id"] = new_val
        common.save_config(self.config)
        self.lbl_id.config(text=new_val)
        self.start_host_service()

    def new_password(self):
        new_val = common.generate_password()
        self.config["my_password"] = new_val
        common.save_config(self.config)
        self.lbl_pw.config(text=new_val)
        self.start_host_service()

    def save_server(self):
        new_server = self.entry_server.get().strip()
        if not new_server.startswith("ws://") and not new_server.startswith("wss://"):
            messagebox.showwarning("Ungültige URL", "Die Server-URL muss mit ws:// oder wss:// beginnen.")
            return
        self.config["relay_server"] = new_server
        common.save_config(self.config)
        self.start_host_service()
        messagebox.showinfo("Gespeichert", f"Relay-Server aktualisiert: {new_server}")

    def on_mode_change(self, event=None):
        val = self.combo_mode.get()
        if "Schnell" in val or "Turbo" in val:
            self.config["quality"] = 65
            self.config["scale"] = 1.0  # Crisp text, never downscale!
        elif "HQ" in val:
            self.config["quality"] = 85
            self.config["scale"] = 1.0
        else:
            self.config["quality"] = 75
            self.config["scale"] = 1.0
        common.save_config(self.config)
        if self.host_service:
            self.host_service.capturer.update_settings(quality=self.config["quality"], scale=self.config["scale"])

    def enable_uac_remote(self):
        if common.set_uac_secure_desktop(False):
            messagebox.showinfo("UAC-Fernsteuerung aktiv", "Erfolgreich! UAC-Dialoge (Ja/Nein) erscheinen ab sofort auf dem Desktop und können aus der Ferne gesteuert werden.")
            if hasattr(self, "btn_uac"):
                self.btn_uac.config(text="✅ UAC aktiv", state="disabled")
        else:
            messagebox.showerror("Fehler", "Konnte UAC-Einstellung nicht ändern (Administrator-Rechte erforderlich).")

    def on_partner_selected(self, event=None):
        pid = self.entry_partner_id.get().strip()
        for p in self.config.get("recent_partners", []):
            if p.get("id") == pid:
                self.entry_partner_pw.delete(0, tk.END)
                self.entry_partner_pw.insert(0, p.get("password", ""))
                break

    def connect_partner(self):
        partner_id = self.entry_partner_id.get().strip()
        partner_pw = self.entry_partner_pw.get().strip()
        
        if not partner_id:
            messagebox.showwarning("Eingabe fehlt", "Bitte gib eine Partner-ID ein.")
            return

        common.remember_partner(self.config, partner_id, partner_pw)
        self.entry_partner_id["values"] = [p["id"] for p in self.config.get("recent_partners", [])]

        relay_url = self.config.get("relay_server", "ws://localhost:8765")
        viewer = RemoteDesktopViewer(self.root, relay_url, partner_id, partner_pw)

    def toggle_windows_service(self):
        if common.is_service_installed():
            if messagebox.askyesno("Dienst entfernen", "Möchtest du den DeskLite-Hintergrunddienst wirklich deinstallieren?"):
                ok, msg = common.uninstall_service()
                if ok:
                    messagebox.showinfo("Deinstalliert", msg)
                    if hasattr(self, "btn_service"):
                        self.btn_service.config(text="⚙️ Als Dienst einrichten")
                else:
                    messagebox.showerror("Fehler", f"Konnte Dienst nicht deinstallieren:\n{msg}")
        else:
            if messagebox.askyesno("Dienst einrichten", "Möchtest du DeskLite als Windows-Hintergrunddienst (SYSTEM) installieren?\n\nDadurch läuft DeskLite dauerhaft im Hintergrund und kann Elevated-Dialoge (UAC, Task-Manager) ohne Einschränkungen fernsteuern."):
                ok, msg = common.install_service()
                if ok:
                    messagebox.showinfo("Installiert", msg)
                    if hasattr(self, "btn_service"):
                        self.btn_service.config(text="🗑️ Dienst entfernen")
                else:
                    messagebox.showerror("Fehler", f"Konnte Dienst nicht installieren:\n{msg}")

    def on_close(self):
        if self.host_service:
            self.host_service.stop()
        self.root.destroy()

def run_as_service():
    """Headless background runner when DeskLite is executed by Windows Service Manager."""
    import time
    cfg = common.load_config()
    host_service = HostService(
        relay_url=cfg.get("relay_server", "ws://localhost:8765"),
        my_id=cfg.get("my_id"),
        my_password=cfg.get("my_password"),
        quality=cfg.get("quality", 75),
        max_fps=cfg.get("max_fps", 30),
        scale=cfg.get("scale", 1.0)
    )
    host_service.start()
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        host_service.stop()

if __name__ == "__main__":
    import sys
    if "--service" in sys.argv:
        run_as_service()
    else:
        common.enable_dpi_awareness()
        root = tk.Tk()
        common.apply_tk_scaling(root)
        app = DeskLiteApp(root)
        root.mainloop()

