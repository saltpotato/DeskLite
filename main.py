import tkinter as tk
from tkinter import ttk, messagebox
import common
from host_service import HostService
from viewer_window import RemoteDesktopViewer

class RustDeskLightApp:
    def __init__(self, root):
        self.root = root
        self.root.title("RustDesk Light - Remote Desktop")
        self.root.geometry("680x480")
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
        header = ttk.Label(self.root, text="⚡ RustDesk Light", style="Header.TLabel")
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
        self.entry_partner_id = ttk.Entry(right_card, font=("Consolas", 14), width=18)
        self.entry_partner_id.pack(anchor=tk.W, pady=(0, 12))
        self.entry_partner_id.bind("<Return>", lambda e: self.connect_partner())

        ttk.Label(right_card, text="Partner-Passwort (falls gesetzt):", style="CardLabel.TLabel").pack(anchor=tk.W, pady=(0, 4))
        self.entry_partner_pw = ttk.Entry(right_card, font=("Segoe UI", 11), width=18, show="*")
        self.entry_partner_pw.pack(anchor=tk.W, pady=(0, 16))
        self.entry_partner_pw.bind("<Return>", lambda e: self.connect_partner())

        btn_connect = ttk.Button(right_card, text="🚀 Verbinden", style="Primary.TButton", command=self.connect_partner)
        btn_connect.pack(anchor=tk.W, ipady=4, fill=tk.X)

        # BOTTOM: Server & Status
        bottom_frame = ttk.Frame(self.root, padding=(20, 10))
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM)

        server_row = ttk.Frame(bottom_frame)
        server_row.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(server_row, text="Relay-Server:").pack(side=tk.LEFT)
        self.entry_server = ttk.Entry(server_row, width=32)
        self.entry_server.insert(0, self.config.get("relay_server", "ws://localhost:8765"))
        self.entry_server.pack(side=tk.LEFT, padx=8)

        btn_save_server = ttk.Button(server_row, text="Server ändern", command=self.save_server)
        btn_save_server.pack(side=tk.LEFT)

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
            quality=self.config.get("quality", 60),
            max_fps=self.config.get("max_fps", 30),
            scale=self.config.get("scale", 0.85),
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

    def connect_partner(self):
        partner_id = self.entry_partner_id.get().strip()
        partner_pw = self.entry_partner_pw.get().strip()
        
        if not partner_id:
            messagebox.showwarning("Eingabe fehlt", "Bitte gib eine Partner-ID ein.")
            return

        relay_url = self.config.get("relay_server", "ws://localhost:8765")
        viewer = RemoteDesktopViewer(self.root, relay_url, partner_id, partner_pw)

    def on_close(self):
        if self.host_service:
            self.host_service.stop()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = RustDeskLightApp(root)
    root.mainloop()
