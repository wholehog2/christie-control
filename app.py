import base64, ipaddress, json, os, re, shutil, sys, threading, time
from pathlib import Path
from PySide6.QtCore import QByteArray, QObject, QPointF, QRectF, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QPlainTextEdit,
                               QPushButton, QScrollArea, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem,
                               QMenu, QMessageBox, QVBoxLayout, QWidget)
from icon_data import ICON_PNG_B64
from protocol import INPUTS, LSE_TEXT, PORT, Projector, wait_for
from themes import THEMES, qss, shut_qss

# CHRISTIE_CONFIG permet de lancer l'appli avec un autre fichier de réglages (ex. pour le simulateur)
CFG = Path(os.environ["CHRISTIE_CONFIG"]).expanduser() if os.environ.get("CHRISTIE_CONFIG") else Path.home() / ".christie_control.json"
BAK = CFG.with_name(CFG.name + ".bak")
DEFAULT = {"theme": "Sombre", "poll": 5, "confirm_off": True, "stagger": 2,
           "projectors": [{"name": "VP 1", "ip": "192.168.0.10", "port": PORT, "inputs": list(INPUTS)}]}
LOAD_WARNING = ""


def load():
    """Lit les réglages. Fichier illisible : il est mis de côté (jamais écrasé), puis on tente la copie de sauvegarde."""
    global LOAD_WARNING
    LOAD_WARNING, moved = "", None
    for path in (CFG, BAK):
        if not path.exists():
            continue
        try:
            cfg = {**json.loads(json.dumps(DEFAULT)), **json.loads(path.read_text("utf-8"))}
        except Exception:
            if path == CFG:
                moved = CFG.with_name(f"{CFG.stem}.corrompu-{time.strftime('%Y%m%d-%H%M%S')}.json")
                try:
                    CFG.rename(moved)
                except OSError:
                    moved = None
            continue
        if path == BAK and moved:
            LOAD_WARNING = (f"Le fichier de réglages était illisible : il a été mis de côté sous « {moved.name} » "
                            "et la dernière sauvegarde a été rechargée.")
        return cfg
    if moved:
        LOAD_WARNING = (f"Le fichier de réglages était illisible : il a été mis de côté sous « {moved.name} » "
                        "et les réglages par défaut sont utilisés.")
    return json.loads(json.dumps(DEFAULT))


def save_cfg(cfg):
    """Écriture atomique ; la version précédente est conservée dans le fichier .bak."""
    tmp = CFG.with_name(CFG.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
    if CFG.exists():
        try:
            shutil.copy2(CFG, BAK)
        except OSError:
            pass
    os.replace(tmp, CFG)


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
    pm = QPixmap()
    pm.loadFromData(base64.b64decode(ICON_PNG_B64), "PNG")
    return QIcon(pm)


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


def confirm(parent, title, text, ok="Oui"):
    """Boîte de confirmation : « Annuler » est le choix par défaut."""
    box = QMessageBox(QMessageBox.Warning, title, text, QMessageBox.NoButton, parent)
    yes = box.addButton(ok, QMessageBox.AcceptRole)
    no = box.addButton("Annuler", QMessageBox.RejectRole)
    box.setDefaultButton(no)
    box.exec()
    return box.clickedButton() is yes


# Commandes (écriture) pouvant faire perdre des réglages ou l'accès au projecteur
DANGEROUS = {
    "DEF": "remet TOUS les réglages du projecteur aux valeurs d'usine",
    "NET": "modifie la configuration réseau : le projecteur peut devenir injoignable",
    "NTW": "modifie la configuration réseau : le projecteur peut devenir injoignable",
    "ADR": "change l'adresse du projecteur : il peut devenir injoignable",
    "BAC": "restaure ou écrase les réglages depuis une sauvegarde",
    "UID": "active un mode de service du projecteur",
    "PCG": "modifie le code PIN du projecteur",
    "PIV": "modifie le code PIN du projecteur",
    "FCT": "modifie des paramètres d'usine",
    "SPP": "change le chemin du port série",
    "WRE": "réinitialise la correction géométrique (warp)",
    "LCB": "recalibre l'optique motorisée",
}


def pretty(label, value):
    """Met en forme une donnée : heures « 2 090 h », températures « 41 °C »."""
    v = str(value).strip()
    if re.fullmatch(r"\d+", v):
        if re.search(r"hour|heure|\bhrs?\b", label, re.I):
            return f"{int(v):,}".replace(",", " ") + " h"
        if re.search(r"temp", label, re.I):
            return v + " °C"
    return v


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
        code = re.sub(r"^[#\s]+", "", c)[:3].upper()
        if code in DANGEROUS and "?" not in c:  # garde-fou : seules les écritures sont filtrées
            if not confirm(self, "Commande risquée", f"« {c} » {DANGEROUS[code]}.\n\nL'envoyer quand même ?", "Envoyer"):
                self.inp.clear()
                self.out.appendPlainText(f"⛔ ({c}) annulée")
                return
        self.inp.clear()
        self.out.appendPlainText(f"→ ({c})")
        quiet = "?" not in c  # écriture : inutile d'attendre une réponse jusqu'au bout
        bg(lambda: self.p.ask(c, timeout=4, quiet=quiet),
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


def svg_icon(svg):
    img = QImage(96, 96, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    QSvgRenderer(QByteArray(svg.encode())).render(p)
    p.end()
    return QIcon(QPixmap.fromImage(img))


def ceiling_svg(c):  # projecteur suspendu au plafond, faisceau vers l'écran
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><g stroke="{c}" fill="none" stroke-width="3" '
            f'stroke-linecap="round"><line x1="5" y1="6" x2="43" y2="6"/><line x1="24" y1="6" x2="24" y2="12"/>'
            f'<line x1="6" y1="43" x2="42" y2="43"/></g><rect x="13" y="12" width="22" height="11" rx="3" fill="{c}"/>'
            f'<polygon points="24,25 11,41 37,41" fill="{c}" opacity=".35"/></svg>')


def rear_svg(c):  # projecteur derrière l'écran, spectateur de l'autre côté
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><g stroke="{c}" fill="none" stroke-width="3" '
            f'stroke-linecap="round"><line x1="27" y1="5" x2="27" y2="43"/></g>'
            f'<rect x="2" y="19" width="14" height="10" rx="3" fill="{c}"/>'
            f'<polygon points="17,24 26,11 26,37" fill="{c}" opacity=".35"/>'
            f'<ellipse cx="39" cy="24" rx="7" ry="4.5" fill="none" stroke="{c}" stroke-width="2.5"/>'
            f'<circle cx="39" cy="24" r="2.2" fill="{c}"/></svg>')


class OriPreview(QWidget):
    """Mini-écran : la lettre F montre comment l'image est retournée (haut/bas, gauche/droite)."""
    def __init__(self, compact=False):
        super().__init__()
        self.flip_v = self.flip_h = False
        self.setFixedSize(*((84, 50) if compact else (140, 82)))

    def set_flips(self, v, h):
        self.flip_v, self.flip_h = v, h
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor("#4f8cff"), 2))
        p.setBrush(QColor("#0f1b33"))
        p.drawRoundedRect(QRectF(2, 2, w - 4, h - 4), 8, 8)
        p.translate(w / 2, h / 2)
        p.scale(-1 if self.flip_h else 1, -1 if self.flip_v else 1)
        f = QFont()
        f.setBold(True)
        f.setPixelSize(int(h * 0.62))
        p.setFont(f)
        p.setPen(QColor("#e6ecff"))
        p.drawText(QRectF(-w / 2, -h / 2, w, h), Qt.AlignCenter, "F")
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#22c55e"))  # repère du coin haut-gauche d'origine
        p.drawEllipse(QPointF(-w / 2 + 13, -h / 2 + 13), 4, 4)


class OrientBtn(Seg):
    """Bouton à bascule avec pictogramme : gris = inactif, plein = actif, orange = en cours, rouge = échec."""
    COL = {"ok": "#ffffff", "busy": "#f59e0b", "err": "#ef4444", "off": "#8b93a7"}

    def __init__(self, text, svg_fn, cb, size):
        super().__init__(text, cb)
        self.svg_fn = svg_fn
        self.setIconSize(QSize(size, size))
        self.setMinimumHeight(size + 22)
        self.set("off")

    def set(self, st):
        self.setIcon(svg_icon(self.svg_fn(self.COL[st])))
        super().set(st)


ORI = {"ceiling": ("CEL", "Au plafond"), "rear": ("SOR", "En rétro")}
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
    "lightdark": "Clair moderne · sombre",
    "lightmid": "Clair moderne · intermédiaire",
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
        self.key = f'{cfg["ip"]}:{cfg.get("port", PORT)}'  # identifiant stable : survit au renommage du VP
        folded = main.cfg.get("folded", [])
        self.folded = self.key in folded or cfg["name"] in folded
        self.locked, self.lockables = False, []
        self.fail, self.poll_n, self.sig, self.last_st, self.lse_text = 0, 0, None, None, ""
        self.pwr_t0 = self.pwr_target = None
        self.pwr_info = QLabel()
        self.pwr_info.setStyleSheet("font-weight:600")
        self.pwr_timer = QTimer(self)
        self.pwr_timer.timeout.connect(self.refresh_info)
        self.state_lbl = None
        self.video_folded = False  # automatique : replié quand la source est identifiée, déplié sinon
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.max_rows = {"classic": 99, "console": 3, "tiles": 99, "side": 99, "mixer": 0}.get(look, 99)
        self.ins = [i for i in cfg.get("inputs", list(INPUTS)) if i in INPUTS]
        self.src_key, self.src_label = None, None  # source identifiée par le VP (n° d'entrée) et son affichage
        self.ori, self.ori_btn, self.ori_prev, self.ori_loaded = {"ceiling": None, "rear": None}, {}, None, False
        getattr(self, "ui_" + look)()
        self.set_locked(main.locked)

    # --- briques communes ---
    def cap(self, t):
        l = QLabel(t)
        l.setObjectName("cap")
        return l

    def reg(self, g, k, w):
        self.groups.setdefault(g, {})[k] = w
        self.lockables.append(w)
        return w

    def F(self, g, k, t):
        return self.reg(g, k, Func(t, lambda: self.go(g, k)))

    def S(self, g, k, t):
        return self.reg(g, k, Seg(t, lambda: self.go(g, k)))

    def round_power(self):
        rp = RoundPower(lambda k: self.go("pwr", k))
        self.groups["pwr"] = {"on": rp.on, "off": rp.off}
        self.lockables.append(rp)
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
            v.addWidget(self.pwr_info)
            return v
        top.addWidget(self.pwr_info)
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
        self.lockables.append(b2)
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
            if kind == "video":
                self.update_src_label()

        def toggle():
            folded = not is_folded()
            if kind == "data":
                self.folded = folded
            else:
                self.video_folded = folded
            if kind == "data":  # le panneau vidéo, lui, est géré automatiquement (non mémorisé)
                states = set(self.main.cfg.get(setting, []))
                (states.add if folded else states.discard)(self.key)
                if not folded:
                    states.discard(self.cfg["name"])  # ancien format (clé = nom)
                self.main.cfg[setting] = sorted(states)
                self.main.save()
            paint()

        b.clicked.connect(toggle)
        if kind == "video":
            self.video_paint = paint
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
        self.src_label = QLabel()
        self.src_label.setTextFormat(Qt.RichText)
        self.src_label.setStyleSheet("font-size:20px")
        self.src_label.setVisible(False)
        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(self.fold_header("Entrée vidéo", body, "video"))
        if not vertical:  # source en grand à côté du titre (sous le titre dans les colonnes étroites)
            head.addWidget(self.src_label)
        head.addStretch()
        v.addLayout(head)
        if vertical:
            v.addWidget(self.src_label)
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
        v.addWidget(self.ori_panel())
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
        v.addWidget(self.ori_panel(compact=True))
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
        v.addWidget(self.ori_panel())
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
        v.addWidget(self.ori_panel(framed=True))
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
        v.addWidget(self.ori_panel(compact=True, vertical=True))
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
                "shut": "#c4210f", "shut_h": "#e03a22",
            },
            "elegant": {
                "bg": "#111318", "card": "#191c22", "panel": "#101216",
                "text": "#f0f1f4", "sub": "#a3a8b2", "accent": "#aeb8c7",
                "border": "#353a44", "btn": "#252a33", "hover": "#343b47",
                "shut": "#8f1d14", "shut_h": "#b02a1d",
            },
            "light": {
                "bg": "#e9eef7", "card": "#f8faff", "panel": "#edf3fc",
                "text": "#17243a", "sub": "#65748b", "accent": "#0878e8",
                "border": "#cbd8eb", "btn": "#e5edf9", "hover": "#d5e4f7",
                "shut": "#d6311b", "shut_h": "#ea4a30",
            },
            "lightdark": {  # même identité que « light », en sombre
                "bg": "#0d1524", "card": "#131f35", "panel": "#101a2d",
                "text": "#e8f0fc", "sub": "#8fa3bf", "accent": "#0878e8",
                "border": "#25395a", "btn": "#1a2a45", "hover": "#243a5c",
                "shut": "#b3200f", "shut_h": "#d4331c",
            },
            "lightmid": {  # luminosité intermédiaire entre « lightdark » et « light »
                "bg": "#46577a", "card": "#566a8e", "panel": "#4e6084",
                "text": "#f6f9ff", "sub": "#d3def2", "accent": "#0a74e0",
                "border": "#7487ad", "btn": "#5b7098", "hover": "#6a80a8",
                "shut": "#c8321a", "shut_h": "#e24a30",
            },
            "industrial": {
                "bg": "#171717", "card": "#222222", "panel": "#191919",
                "text": "#f4f1eb", "sub": "#aaa39a", "accent": "#ff9800",
                "border": "#55402a", "btn": "#303030", "hover": "#414141",
                "shut": "#b3200f", "shut_h": "#d4331c",
            },
            "color": {
                "bg": "#10172b", "card": "#18243d", "panel": "#152d43",
                "text": "#f4f7ff", "sub": "#b9c7e8", "accent": "#25c7db",
                "border": "#3d4d86", "btn": "#263b60", "hover": "#354d7c",
                "shut": "#e0301e", "shut_h": "#f2492f",
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
        extra += shut_qss(c)  # bouton « Fermer » du shutter : couleur chaude propre à la palette
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
        v.addWidget(self.ori_panel(framed=True))
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
        v.addWidget(self.ori_panel(framed=True))

        source = QFrame()
        source.setObjectName("pane")
        sv = QVBoxLayout(source)
        sv.addWidget(self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], vertical=False))
        v.addWidget(source)
        info = QFrame()
        info.setObjectName("pane")
        iv = QVBoxLayout(info)
        iv.addLayout(self.info_col())
        v.addWidget(info, 1)
        self.skin("elegant")

    def ui_light(self, palette="light"):
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
        rv.addWidget(self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], cols=2))
        rv.addStretch()
        body.addWidget(right, 2)
        v.addLayout(body, 2)
        v.addWidget(self.ori_panel(framed=True))

        info = QFrame()
        info.setObjectName("pane")
        iv = QVBoxLayout(info)
        iv.addLayout(self.info_col())
        v.addWidget(info, 1)
        self.skin(palette)

    def ui_lightdark(self):
        """Clair moderne en version sombre (même disposition)."""
        self.ui_light("lightdark")

    def ui_lightmid(self):
        """Clair moderne en version intermédiaire (même disposition)."""
        self.ui_light("lightmid")

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
        self.state_lbl = QLabel()
        self.state_lbl.setTextFormat(Qt.RichText)
        self.state_lbl.setWordWrap(True)
        mv.addWidget(self.state_lbl)
        mv.addStretch()
        self.update_state_lbl()
        top.addWidget(meter, 1)
        v.addLayout(top)

        inputs = QFrame()
        inputs.setObjectName("pane")
        iv = QVBoxLayout(inputs)
        iv.addWidget(self.video_panel([self.S("src", i, INPUTS[i]) for i in self.ins], cols=3))
        v.addWidget(inputs)
        v.addWidget(self.ori_panel(framed=True))
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
        srcv.addWidget(self.video_panel([self.S("src", i, INPUTS[i]) for i in self.ins], cols=3))
        v.addWidget(source)
        v.addWidget(self.ori_panel(framed=True))
        info = QFrame()
        info.setObjectName("pane")
        infov = QVBoxLayout(info)
        infov.addLayout(self.info_col())
        v.addWidget(info, 1)
        self.skin("color")

    # --- actions et état ---
    def go(self, group, key, ask=True):
        if self.main.locked:
            self.status("Interface verrouillée : déverrouille pour envoyer une commande")
            return
        if group == "pwr" and key == "off" and ask and self.main.cfg.get("confirm_off", True):
            if not confirm(self.main, "Éteindre le projecteur",
                           f"Éteindre « {self.cfg['name']} » ?\n\nL'extinction lance un refroidissement qui peut durer plusieurs minutes.",
                           "Éteindre"):
                return
        if group == "src":
            self.act(group, key, f"SIN+MAIN {key}", self.p.source, 10)
        else:
            c, g, t = CMDS[(group, key)]
            self.act(group, key, c, getattr(self.p, g), t)

    def led(self, group, key, state):
        for k, f in self.groups[group].items():
            st = state if k == key else "off"
            shut = (group, k) == ("shu", "closed")
            w = getattr(f, "btn", f)  # Func : le bouton est f.btn ; Seg : c'est f lui-même
            if shut:
                w.setProperty("shut", "1")  # couleur définie par le thème (themes.py / skin)
            f.led.set(st)
            if shut and w is not f:  # Func : le bouton n'a pas de propriété d'état par défaut
                w.setProperty("st", st)
                repolish(w)

    def update_src_label(self):
        """Source identifiée : en grand à côté du titre quand le panneau est replié (ou dès que le signal manque)."""
        lab = self.src_label
        if lab is None:
            return
        k = self.src_key
        show = bool(k in INPUTS and (self.video_folded or self.sig is False))
        if show:
            extra = ""
            if isinstance(self.sig, tuple):
                extra = f'&nbsp;&nbsp;<span style="font-size:13px;color:#8b93a7">{self.sig[0]}×{self.sig[1]}</span>'
            elif self.sig is False:
                extra = '&nbsp;&nbsp;<span style="font-size:13px;color:#f59e0b">pas de signal</span>'
            # pas de point quand le panneau est replié ; point orange seulement déplié et sans signal
            warn = '<span style="color:#f59e0b">●</span>&nbsp;' if (self.sig is False and not self.video_folded) else ""
            lab.setText(f'{warn}<b>{INPUTS[k]}</b>{extra}')
        lab.setVisible(show)

    def set_video_folded(self, folded):
        self.video_folded = folded
        paint = getattr(self, "video_paint", None)
        if paint:
            paint()

    def sync_video(self, key):
        """key = source identifiée par le VP, ou None. Identifiée : panneau replié ; sinon : déplié."""
        was = self.src_key
        self.src_key = key if key in INPUTS else None
        if self.src_key is None:
            self.set_video_folded(False)
        elif was is None:
            self.set_video_folded(True)
        else:
            self.update_src_label()

    # --- position du projecteur : au plafond / en rétro ---
    def ori_panel(self, compact=False, vertical=False, framed=False):
        size = 28 if compact else 40
        self.ori_prev = OriPreview(compact)
        b1 = OrientBtn("Au plafond\nImage à l'envers", ceiling_svg, lambda: self.toggle_ori("ceiling"), size)
        b2 = OrientBtn("En rétro\nImage en miroir", rear_svg, lambda: self.toggle_ori("rear"), size)
        b1.setToolTip("Montage au plafond : retourne l'image de haut en bas (commande CEL)")
        b2.setToolTip("Rétroprojection : inverse l'image de gauche à droite (commande SOR)")
        self.ori_btn = {"ceiling": b1, "rear": b2}
        self.lockables += [b1, b2]
        body = QWidget()
        l = QVBoxLayout(body) if vertical else QHBoxLayout(body)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(10)
        tip = ("Aperçu (pas un bouton) : la lettre F montre comment l'image sera affichée avec les réglages "
               "ci-contre. Le point vert marque le coin haut-gauche de l'image d'origine.")
        self.ori_prev.setToolTip(tip)
        pv = QWidget()
        pl = QVBoxLayout(pv)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(3)
        pl.addWidget(self.ori_prev, 0, Qt.AlignHCenter)
        cap = QLabel("Aperçu" if compact else "Aperçu de l'image")
        cap.setObjectName("sub")
        cap.setAlignment(Qt.AlignCenter)
        cap.setToolTip(tip)
        pl.addWidget(cap)
        l.addWidget(pv, 0, Qt.AlignHCenter if vertical else Qt.AlignVCenter)
        l.addWidget(b1, 1)
        l.addWidget(b2, 1)
        if not vertical:  # boutons de largeur raisonnable même dans les looks très larges
            b1.setMaximumWidth(300)
            b2.setMaximumWidth(300)
            l.addStretch(1)
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.addWidget(self.cap("Position du projecteur"))
        v.addWidget(body)
        self.paint_ori()
        if framed:
            frame = QFrame()
            frame.setObjectName("pane")
            QVBoxLayout(frame).addWidget(panel)
            return frame
        return panel

    def paint_ori(self):
        if self.ori_prev:
            self.ori_prev.set_flips(self.ori["ceiling"] is True, self.ori["rear"] is True)

    def toggle_ori(self, key):
        if self.main.locked:
            self.status("Interface verrouillée : déverrouille pour envoyer une commande")
            return
        g = "ori_" + key
        if g in self.pending:
            return
        code, label = ORI[key]
        target = self.ori.get(key) is not True
        cmd = f"{code} {1 if target else 0}"
        self.pending.add(g)
        self.ori_btn[key].set("busy")

        def done(ok, err):
            self.pending.discard(g)
            good = bool(ok) and not err
            if good:
                self.ori[key] = target
            self.ori_btn[key].set(("ok" if self.ori[key] else "off") if good else "err")
            self.paint_ori()
            self.status(f"{self.cfg['name']} · {label} {'activé' if target else 'désactivé'} : "
                        + ("confirmé" if good else f"non confirmé ({err or 'pas de réponse du VP'})"))
        bg(lambda: self.p.set_and_check(cmd, code, 1 if target else 0), done)

    def apply_ori(self, d, err):
        if err or not d:
            return
        for key, n in d.items():
            if n is None:
                continue  # le VP ne répond pas à cette lecture : on garde l'état connu
            self.ori[key] = True if n == 1 else False if n == 0 else None  # 2 = automatique
            if "ori_" + key not in self.pending and key in self.ori_btn:
                self.ori_btn[key].set("ok" if self.ori[key] else "off")
        self.paint_ori()

    def act(self, group, key, cmd, getter, timeout):
        if group in self.pending:
            return
        self.pending.add(group)
        self.led(group, key, "busy")
        if group == "pwr":
            self.pwr_start(key)

        def work():
            msgs = self.p.ask(cmd, quiet=True)     # 1) on envoie
            bad = [m for m in msgs if "ERR" in m]
            if bad:                                # refus explicite du VP : inutile d'attendre la fin du délai
                txt = re.search(r'"([^"]*)"', bad[0])
                raise RuntimeError("refusé par le projecteur" + (f" ({txt[1]})" if txt else ""))
            return wait_for(getter, key, timeout)  # 2) on vérifie que c'est appliqué

        def done(ok, err):
            self.pending.discard(group)
            if group == "pwr":
                self.pwr_stop()
            good = bool(ok) and not err
            self.led(group, key, "ok" if good else "err")
            if group == "src" and good:
                self.src_key = key
                self.set_video_folded(True)
            self.status(f"{self.cfg['name']} · {cmd} : " + ("confirmé" if good else f"non confirmé ({err or 'délai dépassé'})"))
        bg(work, done)

    def refresh(self, full=False):
        if full:
            self.ori_loaded = False  # relire aussi la position du VP
            self.load_info()         # et les données
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
        name = self.cfg["name"]
        self.last_st = None if err else st
        if err:
            self.fail += 1
            if self.fail == 2:  # deux échecs d'affilée : le VP est considéré hors ligne
                self.main.set_alert("off:" + self.key, "err", f"{name} : hors ligne")
        else:
            if self.fail >= 2:
                self.status(f"{name} : de nouveau en ligne")
            self.fail = 0
            self.main.clear_alert("off:" + self.key)
        for g in self.groups:
            if g in self.pending:
                continue
            self.led(g, None if err else st[g], "ok")
        if "src" not in self.pending:
            self.sync_video(None if err else st.get("src"))
        self.on_signal(None if err else st.get("sig"))
        if not err and "pwr" not in self.pending:  # allumage / refroidissement lancé ailleurs (télécommande...)
            if st["pwr"] == "busy":
                if self.pwr_t0 is None:
                    self.pwr_start(None)
            elif self.pwr_t0 is not None:
                self.pwr_stop()
        self.refresh_info()
        if not err:
            self.poll_n += 1
            if self.poll_n % 6 == 1:  # dernière erreur système : toutes les ~30 s
                bg(self.p.last_error, self.apply_lse)
        if not err and not self.ori_loaded:
            self.ori_loaded = True
            bg(self.p.orientation, self.apply_ori)
        if not err and not self.info_loaded:
            self.info_loaded = True
            self.load_info()
        self.update_state_lbl()

    # --- compte à rebours, signal, erreurs, verrouillage ---
    def pwr_start(self, target):
        self.pwr_target, self.pwr_t0 = target, time.time()
        self.pwr_timer.start(1000)
        self.refresh_info()

    def pwr_stop(self):
        self.pwr_target = self.pwr_t0 = None
        self.pwr_timer.stop()
        self.refresh_info()

    def refresh_info(self):
        """Texte d'état à côté du nom : « Hors ligne » ou « Allumage… 1:10 »."""
        if self.fail >= 2:
            txt, col = "Hors ligne", "#ef4444"
        elif self.pwr_t0 is not None:
            sec = int(time.time() - self.pwr_t0)
            word = {"on": "Allumage", "off": "Extinction"}.get(self.pwr_target, "Transition")
            txt, col = f"{word}… {sec // 60}:{sec % 60:02d}", "#f59e0b"
        else:
            txt, col = "", ""
        self.pwr_info.setText(txt)
        self.pwr_info.setStyleSheet(f"font-weight:600;color:{col}" if col else "font-weight:600")

    def on_signal(self, sig):
        prev, self.sig = self.sig, sig
        k = "sig:" + self.key
        if sig is False and isinstance(prev, tuple):
            self.main.set_alert(k, "warn", f"{self.cfg['name']} : signal perdu sur {INPUTS.get(self.src_key, 'la source')}")
        elif isinstance(sig, tuple):
            self.main.clear_alert(k)
        self.update_src_label()

    def apply_lse(self, res, err):
        if err or res is None:
            return
        n, name = res
        k = "lse:" + self.key
        if not n:
            self.lse_text = ""
            self.main.clear_alert(k)
        else:
            self.lse_text = LSE_TEXT.get(n, name or f"code {n}")
            self.main.set_alert(k, "warn", f"{self.cfg['name']} : dernière erreur système — {self.lse_text}")
        self.update_state_lbl()

    def update_state_lbl(self):
        if self.state_lbl is None:
            return
        st = self.last_st
        col = lambda c, t: f'<span style="color:{c}">{t}</span>'
        if st is None and self.fail == 0:
            link, pw, sig = "connexion…", "—", "—"
        else:
            off = st is None
            link = col("#ef4444", "hors ligne") if off else col("#22c55e", "en ligne")
            pw = "—" if off else {"on": "allumé", "off": "éteint", "busy": "en transition"}.get(st["pwr"], "—")
            sig = f"{self.sig[0]}×{self.sig[1]}" if isinstance(self.sig, tuple) else (
                col("#f59e0b", "pas de signal") if self.sig is False else "—")
        err = col("#f59e0b", self.lse_text) if self.lse_text else "aucune"
        self.state_lbl.setText(f"Liaison : <b>{link}</b><br>Alimentation : <b>{pw}</b><br>"
                               f"Signal : <b>{sig}</b><br>Dernière erreur : <b>{err}</b>")

    def set_locked(self, locked):
        self.locked = locked
        for w in self.lockables:
            w.setEnabled(not locked)

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
        if not err:  # masque les lignes sans donnée ; heures et températures mises en forme
            rows = [(k, pretty(k, v)) for k, v in rows if v != "non disponible"] or [("Données", "aucune donnée")]

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


class InputsCell(QWidget):
    """Cases à cocher des entrées réellement câblées (avec leur nom)."""
    def __init__(self, selected):
        super().__init__()
        l = QHBoxLayout(self)
        l.setContentsMargins(6, 0, 6, 0)
        l.setSpacing(8)
        self.boxes = {}
        for k, n in INPUTS.items():
            cb = QCheckBox(n)
            cb.setChecked(k in selected)
            self.boxes[k] = cb
            l.addWidget(cb)
        l.addStretch()

    def get(self):
        return [k for k, cb in self.boxes.items() if cb.isChecked()]


class Settings(QDialog):
    def __init__(self, cfg, parent):
        super().__init__(parent)
        self.setWindowTitle("Paramètres")
        self.resize(1160, 580)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Projecteurs"))
        self.t = QTableWidget(0, 5)
        self.t.setHorizontalHeaderLabels(["Nom", "Adresse IP", "Port", "Entrées réellement câblées", "Connexion"])
        hh = self.t.horizontalHeader()
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        for c, w in ((0, 130), (1, 140), (2, 64), (4, 125)):
            hh.setSectionResizeMode(c, QHeaderView.Fixed)
            self.t.setColumnWidth(c, w)
        for p in cfg["projectors"]:
            self.add(p)
        v.addWidget(self.t)
        hint = QLabel("Coche les entrées réellement câblées. « Tester » vérifie la connexion avec les réglages de la ligne.")
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
        f.addSpacing(30)
        f.addWidget(QLabel("Décalage entre projecteurs pour « Tout allumer » (s)"))
        self.stagger = QSpinBox()
        self.stagger.setRange(0, 60)
        self.stagger.setValue(cfg.get("stagger", 2))
        f.addWidget(self.stagger)
        f.addStretch()
        v.addLayout(f)
        self.confirm = QCheckBox("Demander confirmation avant d'éteindre un projecteur")
        self.confirm.setChecked(cfg.get("confirm_off", True))
        v.addWidget(self.confirm)
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

    def cell(self, r, c):
        it = self.t.item(r, c)
        return (it.text() if it else "").strip()

    def add(self, p=None):
        r = self.t.rowCount()
        p = p or {"name": f"VP {r + 1}", "ip": "192.168.0.1", "port": PORT, "inputs": list(INPUTS)}
        self.t.insertRow(r)
        for i, x in enumerate([p["name"], p["ip"], str(p.get("port", PORT))]):
            self.t.setItem(r, i, QTableWidgetItem(x))
        self.t.setCellWidget(r, 3, InputsCell(p.get("inputs", list(INPUTS))))
        b = QPushButton("Tester")
        b.clicked.connect(lambda _=False, b=b: self.test_row(b))
        self.t.setCellWidget(r, 4, b)
        self.t.setRowHeight(r, 44)

    def bind_ip(self):
        name = self.iface.currentData()
        return next((i for n, i in net_ifaces() if n == name), None) if name else None

    def test_row(self, btn):
        """Teste la connexion avec les réglages de la ligne (adresse, port, carte réseau choisie)."""
        r = next((i for i in range(self.t.rowCount()) if self.t.cellWidget(i, 4) is btn), None)
        if r is None:
            return
        ip, port = self.cell(r, 1), self.cell(r, 2)
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            btn.setText("IP invalide")
            return
        proj = Projector(ip, int(port) if port.isdigit() else PORT)
        proj.bind = self.bind_ip()
        btn.setEnabled(False)
        btn.setText("Test…")

        def done(st, err):
            btn.setEnabled(True)
            if err:
                btn.setText("✗ Injoignable")
                btn.setToolTip(str(err))
            else:
                btn.setText("✓ En ligne")
                btn.setToolTip(f"Alimentation : {st['pwr']} · source : {INPUTS.get(st['src'], 'aucune')}")
        bg(proj.state, done)

    def accept(self):
        errs, names, addrs = [], set(), set()
        for r in range(self.t.rowCount()):
            name, ip, port = self.cell(r, 0), self.cell(r, 1), self.cell(r, 2)
            if not ip:
                continue  # ligne vide : ignorée
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                errs.append(f"Ligne {r + 1} : adresse IP invalide (« {ip} »)")
            if port and not (port.isdigit() and 1 <= int(port) <= 65535):
                errs.append(f"Ligne {r + 1} : port invalide (« {port} », attendu de 1 à 65535)")
            nm = name or f"VP {r + 1}"
            if nm in names:
                errs.append(f"Ligne {r + 1} : le nom « {nm} » est déjà utilisé")
            names.add(nm)
            ad = (ip, port or str(PORT))
            if ad in addrs:
                errs.append(f"Ligne {r + 1} : cette adresse est déjà utilisée par un autre projecteur")
            addrs.add(ad)
            if not self.t.cellWidget(r, 3).get():
                errs.append(f"Ligne {r + 1} : coche au moins une entrée")
        if errs:
            QMessageBox.warning(self, "Réglages invalides", "\n".join(errs))
            return
        super().accept()

    def projectors(self):
        out = []
        for r in range(self.t.rowCount()):
            ip = self.cell(r, 1)
            if not ip:
                continue
            port = self.cell(r, 2)
            out.append({"name": self.cell(r, 0) or f"VP {r + 1}", "ip": ip, "port": int(port) if port.isdigit() else PORT,
                        "inputs": self.t.cellWidget(r, 3).get() or list(INPUTS)})
        return out


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Christie Control · D20HD-HS")
        self.resize(1200, 820)
        self.cfg, self.cards = load(), []
        self.locked, self.alerts, self.acked, self.side_frame = False, {}, set(), None
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
        rf.clicked.connect(lambda: (self.sync_net(), [c.refresh(True) for c in self.cards]))
        st.clicked.connect(self.settings)
        self.grp_btn = QPushButton("Actions groupées")
        menu = QMenu(self.grp_btn)
        for label, kind in (("Tout allumer", "on"), ("Tout éteindre", "off"),
                            ("Fermer tous les shutters", "shut_close"), ("Ouvrir tous les shutters", "shut_open")):
            act = QAction(label, menu)
            act.triggered.connect(lambda _=False, k=kind: self.group(k))
            menu.addAction(act)
        self.grp_btn.setMenu(menu)
        self.lock_btn = QPushButton("Verrouiller")
        self.lock_btn.setCheckable(True)
        self.lock_btn.setToolTip("Verrouille toutes les commandes pour éviter un clic malheureux")
        self.lock_btn.toggled.connect(self.toggle_lock)
        top.addWidget(t)
        top.addSpacing(12)
        top.addWidget(self.grp_btn)
        top.addWidget(self.lock_btn)
        top.addStretch()
        top.addWidget(QLabel("Look"))
        top.addWidget(self.look)
        top.addWidget(QLabel("Thème"))
        top.addWidget(self.theme)
        top.addWidget(rf)
        top.addWidget(st)
        v.addLayout(top)
        self.alert_box = QFrame()
        self.alert_lay = QVBoxLayout(self.alert_box)
        self.alert_lay.setContentsMargins(0, 0, 0, 0)
        self.alert_lay.setSpacing(6)
        self.alert_box.setVisible(False)
        v.addWidget(self.alert_box)
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
        if LOAD_WARNING:
            QTimer.singleShot(400, self.show_load_warning)

    def set_theme(self, name, save=True):
        # « Sans thème » désactive le QSS global : les looks personnalisés
        # conservent ainsi exactement leur propre palette et les widgets
        # standards utilisent le style natif Fusion.
        if name == getattr(self, "no_theme", "Sans thème"):
            QApplication.instance().setStyleSheet(shut_qss(THEMES["Sombre"]))
        else:
            QApplication.instance().setStyleSheet(qss(THEMES[name]))
        self.cfg["theme"] = name
        self.style_side()
        if save:
            self.save()

    def style_side(self):
        """Barre latérale : le VP sélectionné est mis en avant discrètement (voile gris + fine barre d'accent)."""
        side = getattr(self, "side_frame", None)
        if side is None:
            return
        t = THEMES.get(self.cfg.get("theme"))
        acc = t["accent"] if t else "#4f8cff"
        col = f"color:{t['text']};" if t else ""
        try:
            side.setStyleSheet('QPushButton[sel="1"]{background:rgba(127,127,127,0.22);' + col
                               + f'border-left:3px solid {acc};font-weight:600}}')
        except RuntimeError:  # widget déjà supprimé (changement de look)
            self.side_frame = None

    def save(self):
        save_cfg(self.cfg)

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
        valid = {c.key for c in self.cards}  # alertes : on oublie celles des projecteurs supprimés
        self.alerts = {k: v for k, v in self.alerts.items() if k.split(":", 1)[1] in valid}
        self.render_alerts()
        self.side_frame = None
        h = QWidget()
        if look == "side":
            lay = QHBoxLayout(h)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(16)
            side = QFrame()
            side.setObjectName("card")
            side.setFixedWidth(240)
            self.side_frame = side
            self.style_side()
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

    def toggle_lock(self, on):
        self.locked = on
        self.lock_btn.setText("Déverrouiller" if on else "Verrouiller")
        self.lock_btn.setProperty("st", "busy" if on else "off")
        repolish(self.lock_btn)
        self.grp_btn.setEnabled(not on)
        for c in self.cards:
            c.set_locked(on)
        self.msg.setText("Commandes verrouillées" if on else "Commandes déverrouillées")

    def group(self, kind):
        """Actions sur tous les projecteurs, avec confirmation (et décalage à l'allumage)."""
        if self.locked or not self.cards:
            return
        n, gap = len(self.cards), int(self.cfg.get("stagger", 2))
        spec = {
            "on": ("pwr", "on", "Tout allumer", f"Allumer les {n} projecteur(s), avec {gap} s d'écart entre chacun ?", "Allumer"),
            "off": ("pwr", "off", "Tout éteindre",
                    f"Éteindre les {n} projecteur(s) ?\n\nL'extinction lance un refroidissement qui peut durer plusieurs minutes.", "Tout éteindre"),
            "shut_close": ("shu", "closed", "Fermer tous les shutters", f"Fermer le shutter des {n} projecteur(s) ? L'image sera coupée.", "Fermer"),
            "shut_open": ("shu", "open", "Ouvrir tous les shutters", "", ""),
        }
        g, k, title, text, ok = spec[kind]
        if text and not confirm(self, title, text, ok):
            return
        gap_ms = gap * 1000 if kind == "on" else 0
        for i, c in enumerate(self.cards):
            QTimer.singleShot(i * gap_ms, lambda c=c: self.run_card(c, g, k))
        self.msg.setText(f"{title} : commande envoyée à {n} projecteur(s)" + (f" (décalage de {gap} s)" if gap_ms and n > 1 else ""))

    @staticmethod
    def run_card(card, g, k):
        try:
            card.go(g, k, False)
        except RuntimeError:  # carte supprimée entre-temps (changement de look, réglages...)
            pass

    # --- alertes : bandeau en haut de la fenêtre ---
    def set_alert(self, key, level, text):
        new = key not in self.alerts or self.alerts[key][1] != text
        self.alerts[key] = (level, text)
        if new and (key, text) not in self.acked:
            try:
                QApplication.beep()
            except Exception:
                pass
        self.render_alerts()

    def clear_alert(self, key):
        if self.alerts.pop(key, None) is not None:
            self.acked = {x for x in self.acked if x[0] != key}
            self.render_alerts()

    def ack_alert(self, key):
        if key in self.alerts:
            self.acked.add((key, self.alerts[key][1]))
            self.render_alerts()

    def render_alerts(self):
        while self.alert_lay.count():
            w = self.alert_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        shown = 0
        for key, (level, text) in self.alerts.items():
            if (key, text) in self.acked:
                continue
            border, bgc = {"err": ("#ef4444", "rgba(239,68,68,0.16)"), "warn": ("#f59e0b", "rgba(245,158,11,0.16)")}[level]
            row = QFrame()
            row.setObjectName("alertrow")
            row.setStyleSheet(f"QFrame#alertrow{{background:{bgc};border:1px solid {border};border-radius:8px}}")
            h = QHBoxLayout(row)
            h.setContentsMargins(12, 6, 8, 6)
            h.addWidget(QLabel("⚠  " + text), 1)
            ok = QPushButton("OK")
            ok.setToolTip("J'ai vu : masquer cette alerte (elle reviendra si le problème change)")
            ok.clicked.connect(lambda _=False, k=key: self.ack_alert(k))
            h.addWidget(ok)
            self.alert_lay.addWidget(row)
            shown += 1
        self.alert_box.setVisible(shown > 0)

    def show_load_warning(self):
        box = QMessageBox(QMessageBox.Warning, "Réglages", LOAD_WARNING, QMessageBox.Ok, self)
        box.open()

    def settings(self):
        d = Settings(self.cfg, self)
        if d.exec():
            self.cfg["projectors"], self.cfg["poll"] = d.projectors(), d.poll.value()
            self.cfg["iface"] = d.iface.currentData()
            self.cfg["confirm_off"], self.cfg["stagger"] = d.confirm.isChecked(), d.stagger.value()
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
