import json, re, sys, threading
from pathlib import Path
from PySide6.QtCore import QByteArray, QObject, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QPlainTextEdit,
                               QPushButton, QScrollArea, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)
from protocol import INPUTS, PORT, Projector, wait_for
from themes import THEMES, qss

ICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100"><rect x="2" y="2" width="96" height="96" rx="22" fill="#0f1b33"/><polygon points="43,46 84,26 84,74 43,54" fill="#4f8cff" opacity=".4"/><rect x="14" y="40" width="24" height="20" rx="5" fill="#e6ecff"/><circle cx="38" cy="50" r="7" fill="#4f8cff"/><circle cx="22" cy="46" r="2.5" fill="#22c55e"/><rect x="84" y="24" width="4" height="52" rx="2" fill="#e6ecff"/></svg>'''
CFG = Path.home() / ".christie_control.json"
DEFAULT = {"theme": "Sombre", "poll": 5,
           "projectors": [{"name": "VP 1", "ip": "192.168.0.10", "port": PORT, "inputs": list(INPUTS)}]}


def load():
    try:
        return {**DEFAULT, **json.loads(CFG.read_text("utf-8"))}
    except Exception:
        return json.loads(json.dumps(DEFAULT))


def net_ifaces():
    """Cartes réseau actives avec une adresse IPv4 : [(nom, ip)]."""
    F = QNetworkInterface.InterfaceFlag
    out = []
    for i in QNetworkInterface.allInterfaces():
        f = i.flags()
        if not (f & F.IsUp and f & F.IsRunning) or f & F.IsLoopBack:
            continue
        ips = [e.ip().toString() for e in i.addressEntries()
               if e.ip().protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol]
        if ips:
            out.append((i.humanReadableName(), ips[0]))
    return out


def app_icon():
    r = QSvgRenderer(QByteArray(ICON_SVG.encode()))
    ic = QIcon()
    for s in (16, 32, 64, 128, 256, 512):
        img = QImage(s, s, QImage.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        r.render(p)
        p.end()
        ic.addPixmap(QPixmap.fromImage(img))
    return ic


class Relay(QObject):
    """Ramène les résultats des threads réseau dans le thread de l'interface."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run)

    def _run(self, f):
        try:
            f()
        except RuntimeError:  # widget supprimé entre-temps
            pass


relay = None


def bg(fn, cb):
    def work():
        try:
            r, e = fn(), None
        except Exception as ex:
            r, e = None, ex
        relay.call.emit(lambda: cb(r, e))
    threading.Thread(target=work, daemon=True).start()


class Led(QWidget):
    COL = {"off": "#6b7280", "busy": "#f59e0b", "ok": "#22c55e", "err": "#ef4444"}

    def __init__(self):
        super().__init__()
        self.setFixedSize(16, 16)
        self.state = "off"

    def set(self, state):
        self.state = state
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self.COL[self.state])
        p.setPen(Qt.NoPen)
        if self.state != "off":
            g = QColor(c)
            g.setAlpha(70)
            p.setBrush(g)
            p.drawEllipse(0, 0, 16, 16)
        p.setBrush(c)
        p.drawEllipse(4, 4, 8, 8)


class Func(QWidget):
    """Un bouton de commande accompagné de sa LED."""
    def __init__(self, text, cb):
        super().__init__()
        l = QHBoxLayout(self)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(8)
        self.led = Led()
        self.btn = QPushButton(text)
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.clicked.connect(cb)
        l.addWidget(self.led)
        l.addWidget(self.btn, 1)


class Console(QDialog):
    def __init__(self, proj, name, parent):
        super().__init__(parent)
        self.p = proj
        self.setWindowTitle(f"Console — {name}")
        self.resize(560, 380)
        v = QVBoxLayout(self)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.inp = QLineEdit()
        self.inp.setPlaceholderText("Commande brute, ex : SST?   PWR?   ITP 1   (sans parenthèses)")
        self.inp.returnPressed.connect(self.send)
        v.addWidget(self.out)
        v.addWidget(self.inp)

    def send(self):
        c = self.inp.text().strip().strip("()")
        if not c:
            return
        self.inp.clear()
        self.out.appendPlainText(f"→ ({c})")
        bg(lambda: self.p.ask(c, timeout=4),
           lambda r, e: self.out.appendPlainText(f"✖ {e}" if e else "\n".join(f"← ({m})" for m in r) or "(aucune réponse)"))


def power_icon(col):
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><g fill="none" stroke="{col}" stroke-width="9" '
           'stroke-linecap="round"><path d="M33 31A26 26 0 1 0 67 31"/><path d="M50 16V50"/></g></svg>')
    img = QImage(96, 96, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    QSvgRenderer(QByteArray(svg.encode())).render(p)
    p.end()
    return QIcon(QPixmap.fromImage(img))


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


class Seg(QPushButton):
    """Bouton dont la couleur reflète l'état (off / busy / ok / err) : looks Tuiles et Mixeur."""
    def __init__(self, text, cb):
        super().__init__(text)
        self.led = self
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(cb)
        self.setProperty("st", "off")

    def set(self, st):
        self.setProperty("st", st)
        repolish(self)


class Ring:
    def __init__(self, owner, key):
        self.o, self.k, self.led = owner, key, self

    def set(self, st):
        self.o.states[self.k] = st
        self.o.paint()


class RoundPower(QPushButton):
    """Gros bouton rond : un clic bascule allumé/éteint ; l'anneau reflète l'état vérifié."""
    def __init__(self, toggle):
        super().__init__()
        self.setObjectName("pwr")
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(40, 40))
        self.states = {"on": "off", "off": "off"}
        self.on, self.off = Ring(self, "on"), Ring(self, "off")
        self.clicked.connect(lambda: toggle("off" if self.states["on"] == "ok" else "on"))
        self.paint()

    def paint(self):
        s = self.states.values()
        st = "busy" if "busy" in s else "err" if "err" in s else "ok" if self.states["on"] == "ok" else "off"
        self.setIcon(power_icon({"ok": "#22c55e", "busy": "#f59e0b", "err": "#ef4444", "off": "#8b93a7"}[st]))
        self.setProperty("st", st)
        repolish(self)


CMDS = {("pwr", "on"): ("PWR 1", "power", 150), ("pwr", "off"): ("PWR 0", "power", 240),
        ("shu", "open"): ("SHU 0", "shutter", 8), ("shu", "closed"): ("SHU 1", "shutter", 8)}
LOOKS = {
    "classic": "Classique (origine)",
    "console": "Console de régie",
    "tiles": "Tuiles",
    "side": "Barre latérale",
    "mixer": "Mixeur",
    "blue": "Moderne bleu",
    "elegant": "Sombre élégant",
    "light": "Clair moderne",
    "industrial": "Industriel",
    "color": "Tuiles & couleurs",
}


class Card(QFrame):
    def __init__(self, cfg, status, main, look):
        super().__init__()
        self.setObjectName("card")
        self.cfg, self.status, self.main, self.look = cfg, status, main, look
        self.p = Projector(cfg["ip"], cfg.get("port", PORT))
        self.groups, self.pending, self.busy, self.info_loaded = {}, set(), False, False
        self.dot, self.dot_cb, self.lcd, self.strip = Led(), None, None, None
        self.folded = cfg["name"] in main.cfg.get("folded", [])
        self.video_folded = cfg["name"] in main.cfg.get("video_folded", [])
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.max_rows = {"classic": 99, "console": 3, "tiles": 99, "side": 99, "mixer": 0}.get(look, 99)
        self.ins = [i for i in cfg.get("inputs", list(INPUTS)) if i in INPUTS]
        getattr(self, "ui_" + look)()

    # --- briques communes ---
    def cap(self, t):
        l = QLabel(t)
        l.setObjectName("cap")
        return l

    def reg(self, g, k, w):
        self.groups.setdefault(g, {})[k] = w
        return w

    def F(self, g, k, t):
        return self.reg(g, k, Func(t, lambda: self.go(g, k)))

    def S(self, g, k, t):
        return self.reg(g, k, Seg(t, lambda: self.go(g, k)))

    def round_power(self):
        rp = RoundPower(lambda k: self.go("pwr", k))
        self.groups["pwr"] = {"on": rp.on, "off": rp.off}
        return rp

    def title(self, size, stacked=False):
        n = QLabel(self.cfg["name"])
        n.setObjectName("title")
        n.setStyleSheet(f"font-size:{size}px")
        ip = QLabel(f'{self.cfg["ip"]}:{self.cfg.get("port", PORT)}')
        ip.setObjectName("sub")
        top = QHBoxLayout()
        top.addWidget(self.dot)
        top.addWidget(n)
        if stacked:
            top.addStretch()
            v = QVBoxLayout()
            v.addLayout(top)
            v.addWidget(ip)
            return v
        top.addStretch()
        top.addWidget(ip)
        return top

    def grp(self, title, items, cols=0, vertical=False):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.addWidget(self.cap(title))
        if cols:
            g = QGridLayout()
            g.setSpacing(6)
            for i, x in enumerate(items):
                g.addWidget(x, i // cols, i % cols)
            v.addLayout(g)
        else:
            l = QVBoxLayout() if vertical else QHBoxLayout()
            l.setSpacing(6)
            for x in items:
                l.addWidget(x)
            v.addLayout(l)
        return w

    def bar(self, full=True, vertical=False):
        h = QVBoxLayout() if vertical else QHBoxLayout()
        if full:
            b1 = QPushButton("Données")
            b1.clicked.connect(self.load_info)
            h.addWidget(b1)
        b2 = QPushButton("Console")
        b2.clicked.connect(lambda: Console(self.p, self.cfg["name"], self.main).show())
        h.addWidget(b2)
        return h

    def fold_header(self, title, body, kind="data"):
        """Bouton ▾/▸ avec un état indépendant pour chaque panneau et chaque VP."""
        b = QPushButton()
        b.setObjectName("fold")
        b.setCursor(Qt.PointingHandCursor)
        setting = "folded" if kind == "data" else "video_folded"

        def is_folded():
            return self.folded if kind == "data" else self.video_folded

        def paint():
            folded = is_folded()
            b.setText(("▸ " if folded else "▾ ") + title)
            body.setVisible(not folded)

        def toggle():
            folded = not is_folded()
            if kind == "data":
                self.folded = folded
            else:
                self.video_folded = folded
            states = set(self.main.cfg.get(setting, []))
            (states.add if folded else states.discard)(self.cfg["name"])
            self.main.cfg[setting] = sorted(states)
            self.main.save()
            paint()

        b.clicked.connect(toggle)
        paint()
        return b

    def video_panel(self, items, cols=0, vertical=False, framed=False):
        """Panneau Entrée vidéo repliable, commun aux cinq présentations."""
        body = QWidget()
        if cols:
            layout = QGridLayout(body)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            for i, item in enumerate(items):
                layout.addWidget(item, i // cols, i % cols)
        else:
            layout = QVBoxLayout(body) if vertical else QHBoxLayout(body)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)
            for item in items:
                layout.addWidget(item)

        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.addWidget(self.fold_header("Entrée vidéo", body, "video"))
        v.addWidget(body)

        if framed:
            frame = QFrame()
            frame.setObjectName("pane")
            QVBoxLayout(frame).addWidget(panel)
            return frame
        return panel

    def info_col(self):
        v = QVBoxLayout()
        v.setSpacing(6)
        body = QWidget()
        l = QVBoxLayout(body)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(6)
        l.addLayout(self.grid)
        l.addLayout(self.bar())
        v.addWidget(self.fold_header("Données du projecteur", body))
        v.addWidget(body)
        v.addStretch()
        return v

    # --- les looks ---
    def ui_classic(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(10)
        v.addLayout(self.title(17))
        for cap, items, cols in (
                ("Alimentation", [self.F("pwr", "on", "Allumer"), self.F("pwr", "off", "Éteindre")], 0),
                ("Shutter", [self.F("shu", "open", "Ouvrir"), self.F("shu", "closed", "Fermer")], 0)):
            v.addWidget(self.cap(cap))
            if cols:
                g = QGridLayout()
                g.setSpacing(8)
                for n, w in enumerate(items):
                    g.addWidget(w, n // cols, n % cols)
                v.addLayout(g)
            else:
                r = QHBoxLayout()
                r.setSpacing(12)
                for w in items:
                    r.addWidget(w)
                v.addLayout(r)
        v.addWidget(self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], cols=3))
        v.addLayout(self.info_col())

    def ui_console(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 12)
        v.setSpacing(8)
        h = QHBoxLayout()
        h.setSpacing(22)
        ident = QWidget()
        ident.setFixedWidth(150)
        ident.setLayout(self.title(16, True))
        h.addWidget(ident)
        h.addWidget(self.grp("Alimentation", [self.F("pwr", "on", "Allumer"), self.F("pwr", "off", "Éteindre")]))
        h.addWidget(self.grp("Shutter", [self.F("shu", "open", "Ouvrir"), self.F("shu", "closed", "Fermer")]))
        h.addWidget(self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], cols=3))
        h.addStretch()
        h.addLayout(self.bar(vertical=True))
        v.addLayout(h)
        self.strip = QLabel("Données : …")
        self.strip.setObjectName("sub")
        self.strip.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.fold_header("Données", self.strip))
        row.addWidget(self.strip, 1)
        v.addLayout(row)

    def ui_tiles(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(14)
        v.addLayout(self.title(18))
        r = QHBoxLayout()
        r.setSpacing(20)
        r.addWidget(self.round_power())
        r.addWidget(self.grp("Shutter", [self.S("shu", "open", "Ouvert"), self.S("shu", "closed", "Fermé")]), 1)
        v.addLayout(r)
        v.addWidget(self.video_panel([self.S("src", i, INPUTS[i]) for i in self.ins], cols=3))
        v.addLayout(self.info_col())

    def ui_side(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(26, 22, 26, 22)
        v.setSpacing(16)
        v.addLayout(self.title(26))
        r = QHBoxLayout()
        r.setSpacing(14)
        for g in (self.grp("Alimentation", [self.F("pwr", "on", "Allumer"), self.F("pwr", "off", "Éteindre")], vertical=True),
                  self.grp("Shutter", [self.F("shu", "open", "Ouvrir"), self.F("shu", "closed", "Fermer")], vertical=True)):
            f = QFrame()
            f.setObjectName("pane")
            QVBoxLayout(f).addWidget(g)
            r.addWidget(f, 1)
        r.addWidget(self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins],
                                     cols=2, framed=True), 1)
        v.addLayout(r)
        v.addLayout(self.info_col(), 1)

    def ui_mixer(self):
        self.setMinimumWidth(215)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(8)
        v.addLayout(self.title(15, True))
        v.addWidget(self.round_power(), 0, Qt.AlignHCenter)
        v.addWidget(self.cap("Shutter"))
        v.addWidget(self.S("shu", "open", "Ouvert"))
        v.addWidget(self.S("shu", "closed", "Fermé"))
        v.addWidget(self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins],
                                     vertical=True))
        v.addStretch()
        self.lcd = QLabel("—")
        self.lcd.setObjectName("lcd")
        self.lcd.setAlignment(Qt.AlignCenter)
        v.addWidget(self.lcd)
        v.addLayout(self.bar())

    # --- cinq variantes graphiques supplémentaires ---
    def skin(self, palette):
        """Applique une identité visuelle locale sans modifier le thème global."""
        palettes = {
            "blue": {
                "bg": "#0b1728", "card": "#142943", "panel": "#102238",
                "text": "#edf6ff", "sub": "#9db8d2", "accent": "#08a9ff",
                "border": "#24547b", "btn": "#1c3856", "hover": "#285078",
            },
            "elegant": {
                "bg": "#111318", "card": "#191c22", "panel": "#101216",
                "text": "#f0f1f4", "sub": "#a3a8b2", "accent": "#aeb8c7",
                "border": "#353a44", "btn": "#252a33", "hover": "#343b47",
            },
            "light": {
                "bg": "#e9eef7", "card": "#f8faff", "panel": "#edf3fc",
                "text": "#17243a", "sub": "#65748b", "accent": "#0878e8",
                "border": "#cbd8eb", "btn": "#e5edf9", "hover": "#d5e4f7",
            },
            "industrial": {
                "bg": "#171717", "card": "#222222", "panel": "#191919",
                "text": "#f4f1eb", "sub": "#aaa39a", "accent": "#ff9800",
                "border": "#55402a", "btn": "#303030", "hover": "#414141",
            },
            "color": {
                "bg": "#10172b", "card": "#18243d", "panel": "#152d43",
                "text": "#f4f7ff", "sub": "#b9c7e8", "accent": "#25c7db",
                "border": "#3d4d86", "btn": "#263b60", "hover": "#354d7c",
            },
        }
        c = palettes[palette]
        # La couleur des touches d'entrée est différenciée dans le look multicolore.
        extra = """
QPushButton[st="ok"] { background: #0878e8; color: white; border-color: #0878e8; }
"""
        if palette == "color":
            extra += """
QPushButton[st="ok"] { background: #13a85b; border-color: #13a85b; }
QPushButton#pwr[st="ok"] { border-color: #13c879; }
QPushButton#fold { color: #75d8ee; }
"""
        elif palette == "industrial":
            extra += "QPushButton#fold { color: #ffad32; }\n"
        elif palette == "blue":
            extra += "QPushButton#fold { color: #42c7ff; }\n"
        self.setStyleSheet(f"""
QWidget {{ background: transparent; color: {c['text']}; }}
QFrame#card {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 14px; }}
QFrame#pane {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 11px; }}
QLabel#title {{ color: {c['text']}; font-weight: 650; }}
QLabel#sub, QLabel#cap {{ color: {c['sub']}; }}
QPushButton {{ background: {c['btn']}; color: {c['text']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 8px 12px; }}
QPushButton:hover {{ background: {c['hover']}; }}
QPushButton:pressed, QPushButton#accent {{ background: {c['accent']}; color: white; border-color: {c['accent']}; }}
QPushButton#fold {{ background: transparent; border: none; color: {c['sub']}; font-weight: 650; text-align: left; padding: 3px 0; }}
QPushButton#fold:hover {{ color: {c['text']}; }}
QPushButton[st="ok"] {{ background: {c['accent']}; color: white; border-color: {c['accent']}; }}
QPushButton[st="busy"] {{ border: 2px solid #f59e0b; }}
QPushButton[st="err"] {{ border: 2px solid #ef4444; }}
QPushButton#pwr {{ border: 4px solid {c['border']}; border-radius: 42px; }}
QPushButton#pwr[st="ok"] {{ border-color: #22c55e; }}
QLabel#lcd {{ background: #09110c; color: #67f59a; border-radius: 7px; padding: 8px; }}
{extra}
""")

    def ui_blue(self):
        """Tableau de bord bleu : gros contrôle d'alimentation et panneaux horizontaux."""
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 20, 22, 18)
        v.setSpacing(14)
        v.addLayout(self.title(21))

        top = QHBoxLayout()
        top.setSpacing(18)
        power = QFrame()
        power.setObjectName("pane")
        pv = QVBoxLayout(power)
        pv.addWidget(self.cap("ALIMENTATION"), 0, Qt.AlignHCenter)
        pv.addWidget(self.round_power(), 0, Qt.AlignHCenter)
        top.addWidget(power)

        shutter = QFrame()
        shutter.setObjectName("pane")
        sv = QVBoxLayout(shutter)
        sv.addWidget(self.cap("SHUTTER"))
        sv.addWidget(self.F("shu", "open", "Ouvrir le shutter"))
        sv.addWidget(self.F("shu", "closed", "Fermer le shutter"))
        sv.addStretch()
        top.addWidget(shutter, 1)
        v.addLayout(top)

        video = self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], cols=4, framed=True)
        v.addWidget(video)
        info = QFrame()
        info.setObjectName("pane")
        iv = QVBoxLayout(info)
        iv.addLayout(self.info_col())
        v.addWidget(info, 1)
        self.skin("blue")

    def ui_elegant(self):
        """Style élégant : interface épurée, commandes discrètes et alignées."""
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 22)
        v.setSpacing(18)
        v.addLayout(self.title(23))

        commands = QFrame()
        commands.setObjectName("pane")
        h = QHBoxLayout(commands)
        h.setSpacing(18)
        power = QVBoxLayout()
        power.addWidget(self.cap("ALIMENTATION"))
        power.addWidget(self.F("pwr", "on", "Mise sous tension"))
        power.addWidget(self.F("pwr", "off", "Mise hors tension"))
        h.addLayout(power, 1)
        shutter = QVBoxLayout()
        shutter.addWidget(self.cap("OBTURATEUR"))
        shutter.addWidget(self.F("shu", "open", "Ouvert"))
        shutter.addWidget(self.F("shu", "closed", "Fermé"))
        h.addLayout(shutter, 1)
        v.addWidget(commands)

        source = QFrame()
        source.setObjectName("pane")
        sv = QVBoxLayout(source)
        sv.addWidget(self.cap("SOURCE VIDÉO"))
        sv.addWidget(self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], vertical=False))
        v.addWidget(source)
        info = QFrame()
        info.setObjectName("pane")
        iv = QVBoxLayout(info)
        iv.addLayout(self.info_col())
        v.addWidget(info, 1)
        self.skin("elegant")

    def ui_light(self):
        """Style clair : tableau de bord en deux colonnes avec blocs bien séparés."""
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 20, 22, 18)
        v.setSpacing(14)
        v.addLayout(self.title(22))
        body = QHBoxLayout()
        body.setSpacing(16)

        left = QFrame()
        left.setObjectName("pane")
        lv = QVBoxLayout(left)
        lv.addWidget(self.cap("COMMANDES"))
        lv.addWidget(self.round_power(), 0, Qt.AlignHCenter)
        lv.addSpacing(8)
        lv.addWidget(self.cap("SHUTTER"))
        lv.addWidget(self.F("shu", "open", "Ouvrir"))
        lv.addWidget(self.F("shu", "closed", "Fermer"))
        lv.addStretch()
        body.addWidget(left)

        right = QFrame()
        right.setObjectName("pane")
        rv = QVBoxLayout(right)
        rv.addWidget(self.cap("ENTRÉES VIDÉO"))
        rv.addWidget(self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], cols=2))
        rv.addStretch()
        body.addWidget(right, 2)
        v.addLayout(body, 2)

        info = QFrame()
        info.setObjectName("pane")
        iv = QVBoxLayout(info)
        iv.addLayout(self.info_col())
        v.addWidget(info, 1)
        self.skin("light")

    def ui_industrial(self):
        """Style industriel : pupitre technique, sections encadrées et commandes robustes."""
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)
        v.addLayout(self.title(19, True))

        top = QHBoxLayout()
        top.setSpacing(10)
        power = QFrame()
        power.setObjectName("pane")
        pv = QVBoxLayout(power)
        pv.addWidget(self.cap("POWER"))
        pv.addWidget(self.F("pwr", "on", "ON"))
        pv.addWidget(self.F("pwr", "off", "OFF"))
        top.addWidget(power)
        shutter = QFrame()
        shutter.setObjectName("pane")
        shv = QVBoxLayout(shutter)
        shv.addWidget(self.cap("SHUTTER"))
        shv.addWidget(self.S("shu", "open", "OPEN"))
        shv.addWidget(self.S("shu", "closed", "CLOSE"))
        top.addWidget(shutter)
        meter = QFrame()
        meter.setObjectName("pane")
        mv = QVBoxLayout(meter)
        mv.addWidget(self.cap("ÉTAT"))
        mv.addWidget(QLabel("Contrôle de liaison"), 0, Qt.AlignHCenter)
        mv.addWidget(QLabel("LED d'état dans l'en-tête"), 0, Qt.AlignHCenter)
        top.addWidget(meter, 1)
        v.addLayout(top)

        inputs = QFrame()
        inputs.setObjectName("pane")
        iv = QVBoxLayout(inputs)
        iv.addWidget(self.cap("SÉLECTION DE SOURCE"))
        iv.addWidget(self.video_panel([self.S("src", i, INPUTS[i]) for i in self.ins], cols=3))
        v.addWidget(inputs)
        data = QFrame()
        data.setObjectName("pane")
        dv = QVBoxLayout(data)
        dv.addLayout(self.info_col())
        v.addWidget(data, 1)
        self.skin("industrial")

    def ui_color(self):
        """Style tuiles colorées : chaque commande est un pavé d'état distinct."""
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 18, 18, 18)
        v.setSpacing(12)
        v.addLayout(self.title(20))

        commands = QHBoxLayout()
        commands.setSpacing(12)
        power = QFrame()
        power.setObjectName("pane")
        pv = QVBoxLayout(power)
        pv.addWidget(self.cap("POWER"))
        pv.addWidget(self.round_power(), 0, Qt.AlignHCenter)
        commands.addWidget(power)
        shutter = QFrame()
        shutter.setObjectName("pane")
        sv = QVBoxLayout(shutter)
        sv.addWidget(self.cap("SHUTTER"))
        sv.addWidget(self.S("shu", "open", "OUVERT"))
        sv.addWidget(self.S("shu", "closed", "FERMÉ"))
        sv.addStretch()
        commands.addWidget(shutter, 1)
        v.addLayout(commands)

        source = QFrame()
        source.setObjectName("pane")
        srcv = QVBoxLayout(source)
        srcv.addWidget(self.cap("SOURCES DISPONIBLES"))
        srcv.addWidget(self.video_panel([self.S("src", i, INPUTS[i]) for i in self.ins], cols=3))
        v.addWidget(source)
        info = QFrame()
        info.setObjectName("pane")
        infov = QVBoxLayout(info)
        infov.addLayout(self.info_col())
        v.addWidget(info, 1)
        self.skin("color")

    # --- actions et état ---
    def go(self, group, key):
        if group == "src":
            self.act(group, key, f"SIN+MAIN {key}", self.p.source, 10)
        else:
            c, g, t = CMDS[(group, key)]
            self.act(group, key, c, getattr(self.p, g), t)

    def led(self, group, key, state):
        for k, f in self.groups[group].items():
            f.led.set(state if k == key else "off")

    def act(self, group, key, cmd, getter, timeout):
        if group in self.pending:
            return
        self.pending.add(group)
        self.led(group, key, "busy")

        def work():
            self.p.ask(cmd, quiet=True)            # 1) on envoie
            return wait_for(getter, key, timeout)  # 2) on vérifie que c'est appliqué

        def done(ok, err):
            self.pending.discard(group)
            good = bool(ok) and not err
            self.led(group, key, "ok" if good else "err")
            self.status(f"{self.cfg['name']} · {cmd} : " + ("confirmé" if good else f"non confirmé ({err or 'délai dépassé'})"))
        bg(work, done)

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        bg(self.p.state, self.apply)

    def apply(self, st, err):
        self.busy = False
        s = "err" if err else "ok"
        self.dot.set(s)
        if self.dot_cb:
            self.dot_cb(s)
        for g in self.groups:
            if g in self.pending:
                continue
            self.led(g, None if err else st[g], "ok")
        if not err and not self.info_loaded:
            self.info_loaded = True
            self.load_info()

    def load_info(self):
        bg(self.p.info, self.show_info)

    def show_info(self, rows, err):
        rows = [("Erreur", str(err))] if err else rows

        # Les réponses d'information Christie contiennent parfois le code de
        # paramètre sur trois chiffres, puis la valeur et enfin son libellé.
        # Présenter le libellé dans la colonne de gauche et la valeur à droite.
        # Exemple : "000 2090 Total Projector Hours" ->
        #           "Total Projector Hours" | "2090"
        formatted_rows = []
        for key, value in rows:
            match = re.match(r"^\s*\d{3}\s+(\S+)\s+(.+?)\s*$", str(value))
            if match:
                display_value, display_label = match.groups()
                formatted_rows.append((display_label, display_value))
            else:
                formatted_rows.append((str(key), str(value)))
        rows = formatted_rows

        if self.strip is not None:  # look Console : une ligne de données
            ok = [f"{k} : {v}" for k, v in rows if v != "non disponible"]
            self.strip.setText("   ·   ".join(ok[:5]) or "Aucune donnée disponible")
            return
        if self.lcd is not None:  # look Mixeur : un seul afficheur (1re donnée)
            self.lcd.setText(rows[0][1][:16] if rows else "—")
            self.lcd.setToolTip("\n".join(f"{k} : {v}" for k, v in rows))
            return
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        for i, (k, val) in enumerate(rows[:self.max_rows]):
            a, b = QLabel(k), QLabel(val)
            a.setObjectName("sub")
            b.setWordWrap(True)
            b.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.grid.addWidget(a, i, 0, Qt.AlignTop)
            self.grid.addWidget(b, i, 1)
        self.grid.setColumnStretch(1, 1)


class Settings(QDialog):
    def __init__(self, cfg, parent):
        super().__init__(parent)
        self.setWindowTitle("Paramètres")
        self.resize(680, 460)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Projecteurs"))
        self.t = QTableWidget(0, 4)
        self.t.setHorizontalHeaderLabels(["Nom", "Adresse IP", "Port", "Entrées (n°)"])
        self.t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for p in cfg["projectors"]:
            self.add(p)
        v.addWidget(self.t)
        hint = QLabel("Entrées : " + " · ".join(f"{k} = {n}" for k, n in INPUTS.items()) + "  (celles réellement câblées)")
        hint.setObjectName("sub")
        v.addWidget(hint)
        h = QHBoxLayout()
        a, r = QPushButton("Ajouter un projecteur"), QPushButton("Supprimer la sélection")
        a.clicked.connect(lambda: self.add())
        r.clicked.connect(lambda: [self.t.removeRow(i) for i in sorted({x.row() for x in self.t.selectedIndexes()}, reverse=True)])
        h.addWidget(a)
        h.addWidget(r)
        h.addStretch()
        v.addLayout(h)
        f = QHBoxLayout()
        f.addWidget(QLabel("Actualisation auto (s, 0 = off)"))
        self.poll = QSpinBox()
        self.poll.setRange(0, 300)
        self.poll.setValue(cfg["poll"])
        f.addWidget(self.poll)
        f.addStretch()
        v.addLayout(f)
        n = QHBoxLayout()
        n.addWidget(QLabel("Carte réseau"))
        self.iface = QComboBox()
        self.iface.addItem("Automatique (choix du système)", "")
        for name, ip in net_ifaces():
            self.iface.addItem(f"{name} — {ip}", name)
        cur = cfg.get("iface", "")
        if cur and self.iface.findData(cur) < 0:
            self.iface.addItem(f"{cur} (indisponible)", cur)
        self.iface.setCurrentIndex(max(0, self.iface.findData(cur)))
        n.addWidget(self.iface, 1)
        v.addLayout(n)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def add(self, p=None):
        r = self.t.rowCount()
        p = p or {"name": f"VP {r + 1}", "ip": "192.168.0.1", "port": PORT, "inputs": list(INPUTS)}
        self.t.insertRow(r)
        for i, x in enumerate([p["name"], p["ip"], str(p.get("port", PORT)), ",".join(map(str, p.get("inputs", list(INPUTS))))]):
            self.t.setItem(r, i, QTableWidgetItem(x))

    def projectors(self):
        out = []
        for r in range(self.t.rowCount()):
            g = lambda c: (self.t.item(r, c).text() if self.t.item(r, c) else "").strip()
            if not g(1):
                continue
            ins = [int(x) for x in re.findall(r"\d+", g(3)) if int(x) in INPUTS]
            out.append({"name": g(0) or f"VP {r + 1}", "ip": g(1), "port": int(g(2)) if g(2).isdigit() else PORT,
                        "inputs": ins or list(INPUTS)})
        return out


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Christie Control · D20HD-HS")
        self.resize(1200, 820)
        self.cfg, self.cards = load(), []
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(22, 18, 22, 12)
        top = QHBoxLayout()
        t = QLabel("Projecteurs")
        t.setObjectName("title")
        self.theme = QComboBox()
        self.no_theme = "Sans thème"
        self.theme.addItems([*THEMES, self.no_theme])
        self.theme.setCurrentText(self.cfg["theme"] if self.cfg["theme"] in (*THEMES, self.no_theme) else "Sombre")
        self.theme.currentTextChanged.connect(self.set_theme)
        self.look = QComboBox()
        for k, n in LOOKS.items():
            self.look.addItem(n, k)
        self.look.setCurrentIndex(max(0, self.look.findData(self.cfg.get("look", "classic"))))
        self.look.currentIndexChanged.connect(lambda _: self.set_look(self.look.currentData()))
        rf, st = QPushButton("Actualiser"), QPushButton("Paramètres")
        st.setObjectName("accent")
        rf.clicked.connect(lambda: (self.sync_net(), [c.refresh() for c in self.cards]))
        st.clicked.connect(self.settings)
        for w in (t,):
            top.addWidget(w)
        top.addStretch()
        top.addWidget(QLabel("Look"))
        top.addWidget(self.look)
        top.addWidget(QLabel("Thème"))
        top.addWidget(self.theme)
        top.addWidget(rf)
        top.addWidget(st)
        v.addLayout(top)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        v.addWidget(self.area, 1)
        self.msg = QLabel("Prêt")
        self.msg.setObjectName("sub")
        v.addWidget(self.msg)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: (self.sync_net(), [c.refresh() for c in self.cards]))
        self.set_theme(self.theme.currentText(), save=False)
        self.build()

    def set_theme(self, name, save=True):
        # « Sans thème » désactive le QSS global : les looks personnalisés
        # conservent ainsi exactement leur propre palette et les widgets
        # standards utilisent le style natif Fusion.
        if name == getattr(self, "no_theme", "Sans thème"):
            QApplication.instance().setStyleSheet("")
        else:
            QApplication.instance().setStyleSheet(qss(THEMES[name]))
        self.cfg["theme"] = name
        if save:
            self.save()

    def save(self):
        CFG.write_text(json.dumps(self.cfg, indent=2, ensure_ascii=False), "utf-8")

    def sync_net(self, announce=False):
        """Applique la carte réseau choisie (adresse source des connexions TCP)."""
        name, ip = self.cfg.get("iface", ""), None
        if name:
            ip = next((i for n, i in net_ifaces() if n == name), None)
            if not ip:
                self.msg.setText(f"Carte réseau « {name} » introuvable : réseau automatique")
            elif announce:
                self.msg.setText(f"Réseau : {name} ({ip})")
        for c in self.cards:
            c.p.bind = ip

    def set_look(self, key):
        self.cfg["look"] = key
        self.save()
        self.build()

    def build(self):
        look = self.cfg.get("look", "classic")
        if look not in LOOKS:  # look inconnu (réglages d'une autre version) : retour au classique
            look = "classic"
        self.cards = [Card(c, self.msg.setText, self, look) for c in self.cfg["projectors"]]
        h = QWidget()
        if look == "side":
            lay = QHBoxLayout(h)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(16)
            side = QFrame()
            side.setObjectName("card")
            side.setFixedWidth(240)
            sv = QVBoxLayout(side)
            sv.setSpacing(6)
            stack, items = QStackedWidget(), []

            def pick(i):
                stack.setCurrentIndex(i)
                for j, it in enumerate(items):
                    it.btn.setProperty("sel", "1" if j == i else "0")
                    repolish(it.btn)
            for i, c in enumerate(self.cards):
                it = Func(f'{c.cfg["name"]}  ·  {c.cfg["ip"]}', lambda _=False, i=i: pick(i))
                it.led.set("off")
                c.dot_cb = it.led.set
                items.append(it)
                sv.addWidget(it)
                stack.addWidget(c)
            sv.addStretch()
            lay.addWidget(side)
            lay.addWidget(stack, 1)
            if items:
                pick(0)
        else:
            g = QGridLayout(h)
            g.setSpacing(14)
            positions = {"classic": (lambda n: (n // 2, n % 2)),
                         "console": (lambda n: (n, 0)),
                         "tiles": (lambda n: (n // 2, n % 2)),
                         "mixer": (lambda n: (0, n))}
            position = positions.get(look, positions["classic"])
            for n, c in enumerate(self.cards):
                g.addWidget(c, *position(n))
            if look == "mixer":
                g.setColumnStretch(len(self.cards), 1)
            elif look in ("classic", "tiles") or look not in ("console", "side"):
                g.setRowStretch(len(self.cards) // 2 + 1, 1)
            else:
                g.setRowStretch(len(self.cards), 1)
        self.area.setWidget(h)
        self.timer.stop()
        if self.cfg["poll"] > 0:
            self.timer.start(self.cfg["poll"] * 1000)
        self.sync_net(True)
        for c in self.cards:
            c.refresh()

    def settings(self):
        d = Settings(self.cfg, self)
        if d.exec():
            self.cfg["projectors"], self.cfg["poll"] = d.projectors(), d.poll.value()
            self.cfg["iface"] = d.iface.currentData()
            self.save()
            self.build()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    if sys.platform == "win32":  # icône correcte dans la barre des tâches
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("christie.control.app")
    app.setWindowIcon(app_icon())
    relay = Relay()
    w = Main()
    w.show()
    sys.exit(app.exec())
