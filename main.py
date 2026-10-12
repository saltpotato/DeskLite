import os
import struct
import sys
import time
from PySide6.QtCore import Qt, Signal, QPointF, QRectF, QByteArray, QBuffer, QIODevice
from PySide6.QtGui import (
    QImage, QPixmap, QIcon, QPainter, QPainterPath,
    QColor, QLinearGradient, QPen, QBrush
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QFrame, QMessageBox
)

import common
from host_service import HostService
from viewer_window import RemoteDesktopViewer


def render_app_image(size=512):
    """Render the DeskLite 'Monitor + Drifting Feather' app icon at arbitrary resolution."""
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    scale = size / 512.0
    p.scale(scale, scale)

    # 1. macOS Squircle Background
    rect = QRectF(28, 28, 456, 456)
    bg_grad = QLinearGradient(28, 28, 28, 484)
    bg_grad.setColorAt(0.0, QColor("#1e293b"))
    bg_grad.setColorAt(1.0, QColor("#0f172a"))
    p.setPen(QPen(QColor("#334155"), 6))
    p.setBrush(QBrush(bg_grad))
    p.drawRoundedRect(rect, 102, 102)

    # 2. Smaller Desktop Monitor (compact, generous breathing room)
    mon_rect = QRectF(116, 150, 240, 158)
    scr_grad = QLinearGradient(116, 150, 356, 308)
    scr_grad.setColorAt(0.0, QColor("#09101d"))
    scr_grad.setColorAt(1.0, QColor("#132847"))

    bezel_grad = QLinearGradient(116, 150, 116, 356)
    bezel_grad.setColorAt(0.0, QColor("#94a3b8"))
    bezel_grad.setColorAt(1.0, QColor("#64748b"))

    p.setPen(QPen(QBrush(bezel_grad), 11, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(QBrush(scr_grad))
    p.drawRoundedRect(mon_rect, 20, 20)

    # Subtle inner screen cyan rim
    p.setPen(QPen(QColor(56, 189, 248, 55), 2.5))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(mon_rect.adjusted(9, 9, -9, -9), 12, 12)

    # Monitor Stand & Base
    p.setPen(QPen(QBrush(bezel_grad), 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(236, 312), QPointF(236, 350))
    p.drawLine(QPointF(186, 352), QPointF(286, 352))

    # 3. Smaller minimalist feather resting lightly on the right edge of the monitor
    p.save()
    p.translate(352, 232)
    p.rotate(-28)
    p.scale(0.68, 0.68)

    feather = QPainterPath()
    feather.moveTo(-105, 4)
    feather.cubicTo(-65, -36, -5, -44, 32, -36)
    feather.lineTo(22, -20)
    feather.lineTo(44, -33)
    feather.cubicTo(82, -26, 114, -8, 116, 6)
    feather.cubicTo(108, 24, 55, 40, -15, 34)
    feather.cubicTo(-60, 30, -90, 18, -105, 4)
    feather.closeSubpath()

    # Soft drop shadow across the right monitor edge
    p.save()
    p.translate(-6, 14)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0, 125))
    p.drawPath(feather)
    p.restore()

    # Feather fill gradient (Sky -> Cyan -> Ice White)
    f_grad = QLinearGradient(-105, 0, 116, 0)
    f_grad.setColorAt(0.0, QColor("#0ea5e9"))
    f_grad.setColorAt(0.55, QColor("#38bdf8"))
    f_grad.setColorAt(1.0, QColor("#f0f9ff"))
    p.setPen(QPen(QColor("#e0f2fe"), 3.5))
    p.setBrush(QBrush(f_grad))
    p.drawPath(feather)

    # Gently curved quill shaft
    quill = QPainterPath()
    quill.moveTo(-136, -6)
    quill.cubicTo(-65, 14, 20, 14, 92, 4)
    p.setPen(QPen(QColor("#ffffff"), 6.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(quill)

    p.restore()
    p.end()
    return img


def create_app_icon():
    """Build a multi-resolution QIcon for macOS Dock & Windows Taskbar/Titlebar."""
    icon = QIcon()
    for sz in (16, 24, 32, 48, 64, 128, 256, 512):
        icon.addPixmap(QPixmap.fromImage(render_app_image(sz)))
    return icon


def create_app_pixmap(logical_size=26):
    """Create a Retina @2x QPixmap for inline UI headers."""
    px = QPixmap.fromImage(render_app_image(logical_size * 2))
    px.setDevicePixelRatio(2.0)
    return px


def _image_to_png_bytes(img):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def export_icon_files(ico_path="desklite.ico", icns_path="desklite.icns"):
    """Export native Windows .ico and macOS .icns files without external dependencies."""
    # 1. Windows .ico (multi-size PNG-in-ICO container)
    ico_sizes = (16, 32, 48, 64, 256)
    png_blobs = [(_sz, _image_to_png_bytes(render_app_image(_sz))) for _sz in ico_sizes]
    header = struct.pack("<HHH", 0, 1, len(png_blobs))
    offset = 6 + 16 * len(png_blobs)
    entries = []
    for sz, blob in png_blobs:
        dim = 0 if sz >= 256 else sz
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
    with open(ico_path, "wb") as f:
        f.write(header + b"".join(entries) + b"".join(b for _, b in png_blobs))

    # 2. macOS .icns (Apple ICNS container with ic07=128, ic08=256, ic09=512 PNG chunks)
    icns_Spec = ((b"ic07", 128), (b"ic08", 256), (b"ic09", 512))
    chunks = []
    for tag, sz in icns_Spec:
        blob = _image_to_png_bytes(render_app_image(sz))
        chunks.append(tag + struct.pack(">I", 8 + len(blob)) + blob)
    body = b"".join(chunks)
    with open(icns_path, "wb") as f:
        f.write(b"icns" + struct.pack(">I", 8 + len(body)) + body)


class DeskLiteApp(QMainWindow):
    status_sig = Signal(str, str)

    def __init__(self):
        super().__init__()
        is_sys = common.is_system()
        is_adm = common.is_admin()
        role_tag = "[SYSTEM]" if is_sys else ("[Administrator]" if is_adm and sys.platform == "win32" else "")
        self.setWindowTitle(f"DeskLite {role_tag} - Remote Desktop".replace("  ", " "))
        self.setFixedSize(640, 410)

        self.config = common.load_config()
        self.host_service = None
        self.active_viewers = []

        self.status_sig.connect(self._apply_host_status)
        self._build_ui()
        self.start_host_service()

    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        central.setStyleSheet("""
            QWidget {
                background-color: #f4f6f9;
                color: #1e293b;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                font-size: 13px;
            }
            QFrame#Card {
                background-color: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
            }
            QFrame#Card QLabel {
                background-color: transparent;
            }
            QLineEdit, QComboBox {
                background-color: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 5px;
                padding: 5px 8px;
                font-size: 13px;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #2563eb;
            }
            QPushButton {
                background-color: #e2e8f0;
                color: #1e293b;
                border: 1px solid #cbd5e1;
                border-radius: 5px;
                padding: 5px 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #cbd5e1;
            }
            QPushButton#PrimaryBtn {
                background-color: #2563eb;
                color: #ffffff;
                border: none;
                padding: 8px 16px;
                font-size: 14px;
                font-weight: 700;
            }
            QPushButton#PrimaryBtn:hover {
                background-color: #1d4ed8;
            }
        """)

        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(20, 16, 20, 14)
        root_layout.setSpacing(12)

        # Header with custom Monitor + Feather icon
        header_row = QHBoxLayout()
        header_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_row.setSpacing(8)
        icon_lbl = QLabel()
        icon_lbl.setPixmap(create_app_pixmap(28))
        header_row.addWidget(icon_lbl)

        header_text = "DeskLite"
        if common.is_system():
            header_text += " [SYSTEM]"
        elif common.is_admin() and sys.platform == "win32":
            header_text += " [Administrator]"
        header = QLabel(header_text)
        header.setStyleSheet("font-size: 20px; font-weight: 800; color: #0f172a;")
        header_row.addWidget(header)
        root_layout.addLayout(header_row)

        # Two-column card container
        cards_row = QHBoxLayout()
        cards_row.setSpacing(14)

        # LEFT CARD: Share This PC (Host)
        left_card = QFrame()
        left_card.setObjectName("Card")
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(16, 14, 16, 14)
        left_layout.setSpacing(6)

        lbl_host_title = QLabel("Share This PC")
        lbl_host_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #1d4ed8;")
        left_layout.addWidget(lbl_host_title)

        lbl_id_cap = QLabel("Your ID:")
        lbl_id_cap.setStyleSheet("color: #64748b; font-size: 12px;")
        left_layout.addWidget(lbl_id_cap)

        self.lbl_id = QLabel(self.config.get("my_id", "---"))
        self.lbl_id.setStyleSheet("font-family: Consolas, Menlo, monospace; font-size: 22px; font-weight: 800; color: #15803d;")
        left_layout.addWidget(self.lbl_id)

        id_btn_row = QHBoxLayout()
        btn_copy_id = QPushButton("📋 Copy")
        btn_copy_id.clicked.connect(self.copy_id)
        btn_new_id = QPushButton("🔄 New")
        btn_new_id.clicked.connect(self.new_id)
        id_btn_row.addWidget(btn_copy_id)
        id_btn_row.addWidget(btn_new_id)
        id_btn_row.addStretch(1)
        left_layout.addLayout(id_btn_row)

        left_layout.addSpacing(6)

        lbl_pw_cap = QLabel("One-Time Password:")
        lbl_pw_cap.setStyleSheet("color: #64748b; font-size: 12px;")
        left_layout.addWidget(lbl_pw_cap)

        pw_row = QHBoxLayout()
        self.lbl_pw = QLabel(self.config.get("my_password", "---"))
        self.lbl_pw.setStyleSheet("font-family: Consolas, Menlo, monospace; font-size: 17px; font-weight: 700; color: #c2410c;")
        btn_new_pw = QPushButton("🔄 New")
        btn_new_pw.clicked.connect(self.new_password)
        pw_row.addWidget(self.lbl_pw)
        pw_row.addSpacing(8)
        pw_row.addWidget(btn_new_pw)
        pw_row.addStretch(1)
        left_layout.addLayout(pw_row)
        left_layout.addStretch(1)

        cards_row.addWidget(left_card, 1)

        # RIGHT CARD: Control Remote PC (Viewer)
        right_card = QFrame()
        right_card.setObjectName("Card")
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(16, 14, 16, 14)
        right_layout.setSpacing(6)

        lbl_view_title = QLabel("Control Remote PC")
        lbl_view_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #1d4ed8;")
        right_layout.addWidget(lbl_view_title)

        lbl_pid_cap = QLabel("Partner ID:")
        lbl_pid_cap.setStyleSheet("color: #64748b; font-size: 12px;")
        right_layout.addWidget(lbl_pid_cap)

        self.entry_partner_id = QComboBox()
        self.entry_partner_id.setEditable(True)
        recent_ids = [p["id"] for p in self.config.get("recent_partners", []) if "id" in p]
        self.entry_partner_id.addItems(recent_ids)
        self.entry_partner_id.currentTextChanged.connect(self.on_partner_selected)
        right_layout.addWidget(self.entry_partner_id)

        right_layout.addSpacing(4)

        lbl_ppw_cap = QLabel("Partner Password (if set):")
        lbl_ppw_cap.setStyleSheet("color: #64748b; font-size: 12px;")
        right_layout.addWidget(lbl_ppw_cap)

        self.entry_partner_pw = QLineEdit()
        self.entry_partner_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.entry_partner_pw.returnPressed.connect(self.connect_partner)
        right_layout.addWidget(self.entry_partner_pw)

        if recent_ids:
            self.entry_partner_id.setCurrentIndex(0)
            self.on_partner_selected()

        right_layout.addStretch(1)

        btn_connect = QPushButton("🚀 Connect")
        btn_connect.setObjectName("PrimaryBtn")
        btn_connect.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_connect.clicked.connect(self.connect_partner)
        right_layout.addWidget(btn_connect)

        cards_row.addWidget(right_card, 1)
        root_layout.addLayout(cards_row, 1)

        # BOTTOM ROW 1: ID Server & Profile
        server_row = QHBoxLayout()
        server_row.setSpacing(6)
        server_row.addWidget(QLabel("ID Server:"))
        self.entry_server = QLineEdit(self.config.get("relay_server", "ws://localhost:8765"))
        server_row.addWidget(self.entry_server, 1)

        btn_save_server = QPushButton("Save")
        btn_save_server.clicked.connect(self.save_server)
        server_row.addWidget(btn_save_server)

        server_row.addSpacing(8)
        server_row.addWidget(QLabel("Profile:"))
        self.combo_mode = QComboBox()
        self.combo_mode.addItems(["🚀 Fast", "⚖️ Balanced", "💎 HQ"])
        curr_q = self.config.get("quality", 75)
        if curr_q <= 65:
            self.combo_mode.setCurrentIndex(0)
        elif curr_q >= 85:
            self.combo_mode.setCurrentIndex(2)
        else:
            self.combo_mode.setCurrentIndex(1)
        self.combo_mode.currentIndexChanged.connect(self.on_mode_change)
        server_row.addWidget(self.combo_mode)
        root_layout.addLayout(server_row)

        # BOTTOM ROW 2: Platform / SYSTEM & UAC Controls
        service_row = QHBoxLayout()
        service_row.setSpacing(8)
        if sys.platform == "win32":
            if common.is_system():
                lbl_sys = QLabel("🛡️ SYSTEM Mode Active (UAC controllable)")
                lbl_sys.setStyleSheet("color: #15803d; font-weight: 700; font-size: 12px;")
                service_row.addWidget(lbl_sys)
            else:
                self.btn_service = QPushButton("⚡ Restart as SYSTEM (for UAC)")
                self.btn_service.clicked.connect(self.switch_to_system)
                service_row.addWidget(self.btn_service)

            sec_desk_on = common.get_uac_secure_desktop()
            uac_text = "🔓 Make UAC Visible in Stream" if sec_desk_on else "↩️ Restore UAC Screen Dimming"
            self.btn_uac = QPushButton(uac_text)
            self.btn_uac.clicked.connect(self.toggle_uac_setting)
            service_row.addWidget(self.btn_uac)
        elif sys.platform == "darwin":
            lbl_mac = QLabel("🍏 macOS Native Mode (Qt6 Retina + CoreGraphics + Quartz)")
            lbl_mac.setStyleSheet("color: #1d4ed8; font-weight: 700; font-size: 12px;")
            service_row.addWidget(lbl_mac)
        service_row.addStretch(1)
        root_layout.addLayout(service_row)

        # Status Bar
        self.lbl_status = QLabel("🟡 Initializing...")
        self.lbl_status.setStyleSheet("font-size: 12px; font-weight: 600; color: #b45309;")
        root_layout.addWidget(self.lbl_status)

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
        self.status_sig.emit(status, detail)

    def _apply_host_status(self, status, detail):
        if status == "online":
            self.lbl_status.setText(f"🟢 {detail}")
            self.lbl_status.setStyleSheet("font-size: 12px; font-weight: 600; color: #15803d;")
        elif status == "connecting":
            self.lbl_status.setText(f"🟡 {detail}")
            self.lbl_status.setStyleSheet("font-size: 12px; font-weight: 600; color: #b45309;")
        elif status == "busy":
            self.lbl_status.setText(f"🔵 {detail}")
            self.lbl_status.setStyleSheet("font-size: 12px; font-weight: 600; color: #1d4ed8;")
        elif status in ("error", "disconnected"):
            self.lbl_status.setText(f"🔴 {detail}")
            self.lbl_status.setStyleSheet("font-size: 12px; font-weight: 600; color: #b91c1c;")

    def copy_id(self):
        QApplication.clipboard().setText(self.config.get("my_id", ""))
        QMessageBox.information(self, "Copied", "Your ID has been copied to the clipboard!")

    def new_id(self):
        new_val = common.generate_id()
        self.config["my_id"] = new_val
        common.save_config(self.config)
        self.lbl_id.setText(new_val)
        self.start_host_service()

    def new_password(self):
        new_val = common.generate_password()
        self.config["my_password"] = new_val
        common.save_config(self.config)
        self.lbl_pw.setText(new_val)
        self.start_host_service()

    def save_server(self):
        new_server = self.entry_server.text().strip()
        if not new_server.startswith("ws://") and not new_server.startswith("wss://"):
            QMessageBox.warning(self, "Invalid URL", "The server URL must start with ws:// or wss://.")
            return
        self.config["relay_server"] = new_server
        common.save_config(self.config)
        self.start_host_service()
        QMessageBox.information(self, "Saved", f"ID / Rendezvous server updated: {new_server}")

    def on_mode_change(self, index=None):
        val = self.combo_mode.currentText()
        if "Fast" in val:
            self.config["quality"] = 65
            self.config["scale"] = 1.0
        elif "HQ" in val:
            self.config["quality"] = 85
            self.config["scale"] = 1.0
        else:
            self.config["quality"] = 75
            self.config["scale"] = 1.0
        common.save_config(self.config)
        if self.host_service:
            self.host_service.capturer.update_settings(quality=self.config["quality"], scale=self.config["scale"])

    def switch_to_system(self):
        if not common.elevate_to_system():
            QMessageBox.warning(
                self,
                "Elevation Failed",
                "Could not switch to SYSTEM mode (Administrator privileges required)."
            )

    def toggle_uac_setting(self):
        sec_desk_on = common.get_uac_secure_desktop()
        if sec_desk_on:
            if common.set_uac_secure_desktop(False):
                self.config["uac_secure_desktop"] = False
                common.save_config(self.config)
                QMessageBox.information(
                    self,
                    "UAC Stream Enabled",
                    "UAC prompts will now appear directly on the normal desktop so they are visible and clickable in the remote stream."
                )
                if hasattr(self, "btn_uac"):
                    self.btn_uac.setText("↩️ Restore UAC Screen Dimming")
            else:
                QMessageBox.critical(self, "Error", "Could not update UAC setting (Administrator privileges required).")
        else:
            if common.set_uac_secure_desktop(True):
                self.config["uac_secure_desktop"] = True
                common.save_config(self.config)
                QMessageBox.information(
                    self,
                    "UAC Default Restored",
                    "Windows Secure Desktop dimming for UAC prompts has been restored."
                )
                if hasattr(self, "btn_uac"):
                    self.btn_uac.setText("🔓 Make UAC Visible in Stream")
            else:
                QMessageBox.critical(self, "Error", "Could not update UAC setting (Administrator privileges required).")

    def on_partner_selected(self, text=None):
        pid = self.entry_partner_id.currentText().strip()
        for p in self.config.get("recent_partners", []):
            if p.get("id") == pid:
                self.entry_partner_pw.setText(p.get("password", ""))
                break

    def connect_partner(self):
        partner_id = self.entry_partner_id.currentText().strip()
        partner_pw = self.entry_partner_pw.text().strip()

        if not partner_id:
            QMessageBox.warning(self, "Missing Input", "Please enter a Partner ID.")
            return

        common.remember_partner(self.config, partner_id, partner_pw)
        self.entry_partner_id.blockSignals(True)
        self.entry_partner_id.clear()
        self.entry_partner_id.addItems([p["id"] for p in self.config.get("recent_partners", [])])
        self.entry_partner_id.setCurrentText(partner_id)
        self.entry_partner_id.blockSignals(False)

        relay_url = self.config.get("relay_server", "ws://localhost:8765")
        viewer = RemoteDesktopViewer(self, relay_url, partner_id, partner_pw)
        self.active_viewers.append(viewer)

    def closeEvent(self, event):
        try:
            if self.host_service:
                self.host_service.stop()
        except Exception:
            pass
        os._exit(0)


def run_as_service():
    """Headless background runner when DeskLite is executed by Windows Service Manager."""
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
    if "--export-icons" in sys.argv:
        _app = QApplication(sys.argv)
        export_icon_files()
        sys.exit(0)
    elif "--service" in sys.argv:
        run_as_service()
    else:
        # 0. If a DeskLite window is already running, focus it immediately and exit
        if common.focus_existing_instance():
            sys.exit(0)

        # 1. Enforce Administrator privileges on start (via pythonw.exe so no console stays open)
        if not common.is_admin():
            common.restart_as_admin()

        # 2. Automatically elevate from Admin to interactive SYSTEM in the current session
        #    so DeskLite has the same integrity level (0x4000) as consent.exe (UAC dialogs)
        if common.is_admin() and not common.is_system() and "--system" not in sys.argv:
            common.elevate_to_system()

        # 3. Acquire single-instance mutex so multiple host instances can never conflict
        common.ensure_single_instance()

        # 4. Clean up any broken legacy service entry & ensure normal UAC prompt (never auto-confirm!)
        common.cleanup_broken_service()
        cfg = common.load_config()
        if not cfg.get("uac_secure_desktop", False):
            common.set_uac_secure_desktop(False)

        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DeskLite.RemoteDesktop.1")
            except Exception:
                pass

        app = QApplication(sys.argv)
        app.setApplicationName("DeskLite")
        app.setApplicationDisplayName("DeskLite")
        app_icon = create_app_icon()
        app.setWindowIcon(app_icon)

        window = DeskLiteApp()
        window.setWindowIcon(app_icon)
        window.show()
        sys.exit(app.exec())

