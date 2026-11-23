import base64, ipaddress, json, os, re, shutil, sys, threading, time
from pathlib import Path
from PySide6.QtCore import QByteArray, QObject, QPointF, QRectF, QSize, QTimer, QVariantAnimation, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QImage, QPainter, QPalette, QPen, QPixmap
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGraphicsOpacityEffect, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QPlainTextEdit,
                               QPushButton, QScrollArea, QSlider, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem,
                               QMenu, QMessageBox, QSizePolicy, QVBoxLayout, QWidget)
from icon_data import ICON_PNG_B64
import protocol
from protocol import INPUTS, LSE_TEXT, PORT, Projector, parse, reply_state, wait_for
import webbrowser
from sniffer import NPCAP_URL, NetListener, npcap_installed
from themes import THEMES, qss, shut_qss, slider_qss

# CHRISTIE_CONFIG permet de lancer l'appli avec un autre fichier de réglages (ex. pour le simulateur)
CFG = Path(os.environ["CHRISTIE_CONFIG"]).expanduser() if os.environ.get("CHRISTIE_CONFIG") else Path.home() / ".christie_control.json"
BAK = CFG.with_name(CFG.name + ".bak")
DEFAULT = {"theme": "Sombre", "poll": 5, "confirm_off": True, "stagger": 2, "listen": True,
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


class ActivityLed(QWidget):
    """Voyant d'activité : s'allume un court instant à chaque passage de données, puis s'éteint en fondu."""

    def __init__(self, color, tip):
        super().__init__()
        self.color, self.level = QColor(color), 0.0
        self.setFixedSize(16, 16)
        self.setToolTip(tip)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._fade)

    def flash(self):
        self.level = 1.0
        if not self.timer.isActive():
            self.timer.start(30)
        self.update()

    def _fade(self):
        self.level = max(0.0, self.level - 0.12)
        if self.level == 0.0:
            self.timer.stop()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        off = QColor(self.color)
        off.setAlpha(60)  # éteint : couleur très atténuée, pour qu'on sache laquelle est laquelle
        p.setBrush(off)
        p.drawEllipse(4, 4, 8, 8)
        if self.level > 0:
            g = QColor(self.color)
            g.setAlpha(int(90 * self.level))
            p.setBrush(g)
            p.drawEllipse(0, 0, 16, 16)
            on = QColor(self.color)
            on.setAlpha(int(255 * self.level))
            p.setBrush(on)
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
        # on attend la réponse (jusqu'à 3,5 s) même pour une écriture : un VP lent répond parfois après 0,5 s
        def send_cmd():
            with self.p.priority():  # la commande de l'utilisateur passe avant les lectures de fond
                return self.p.ask(c, timeout=3.5, idle=0.5)
        bg(send_cmd,
           lambda r, e: self.out.appendPlainText(f"✖ {e}" if e else "\n".join(f"← ({m})" for m in r)
                                                 or "(aucune réponse du projecteur dans les 3,5 s)"))


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
        st = ("busy" if "busy" in s else "err" if "err" in s else "ok" if self.states["on"] == "ok"
              else "down" if self.states["off"] == "ok" else "off")   # down = éteint et confirmé : anneau rouge
        self.setIcon(power_icon({"ok": "#22c55e", "busy": "#f59e0b", "err": "#ef4444", "down": "#ef4444", "off": "#8b93a7"}[st]))
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


def lens_svg(c):  # objectif : cercles concentriques
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><g fill="none" stroke="{c}" stroke-width="3">'
            f'<circle cx="24" cy="24" r="16"/><circle cx="24" cy="24" r="8"/></g><circle cx="24" cy="24" r="3" fill="{c}"/>'
            f'<g stroke="{c}" stroke-width="3" stroke-linecap="round"><path d="M24 2V6M24 42V46M2 24H6M42 24H46"/></g></svg>')


def center_svg(c):  # centrer le décalage (shift) : cadre avec flèches vers le centre
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><g stroke="{c}" fill="none" stroke-width="3" '
            f'stroke-linecap="round"><rect x="5" y="5" width="38" height="38" rx="6"/><path d="M24 12V19M24 36V29M12 24H19M36 24H29"/>'
            f'</g><circle cx="24" cy="24" r="3" fill="{c}"/></svg>')


def lock_svg(c, closed):
    shackle = "M16 22V16a8 8 0 0 1 16 0v6" if closed else "M16 22V16a8 8 0 0 1 15-4"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><path d="{shackle}" fill="none" stroke="{c}" '
            f'stroke-width="4" stroke-linecap="round"/><rect x="10" y="22" width="28" height="20" rx="4" fill="{c}"/></svg>')


# Vitesses des ventilateurs (tr/min) pour les couleurs du vumètre.
# Christie ne publie pas de vitesse nominale pour le D20HD-HS ; ces valeurs viennent des seuils d'alerte du manuel
# de service HS 2K (alerte « ventilateur trop lent » entre ~1 960 et ~2 870 tr/min selon le ventilateur, soit
# environ 75 % de la vitesse normale) et sont à ajuster d'après les valeurs lues sur ton projecteur.
FAN_LOW = 2000        # en dessous : ventilateur trop lent (valeur affichée en rouge)
FAN_GREEN_MAX = 4200  # jusqu'ici : fonctionnement normal (vert)
FAN_AMBER_MAX = 4800  # jusqu'ici : élevé (orange) ; au-dessus : trop rapide (rouge)


class VuMeter(QWidget):
    """Vumètre à segments (ventilateur) : vert → ambre → rouge, repère de crête, valeur animée."""
    SEG = 12   # nombre minimal de barres
    BAR = 4    # hauteur visée d'une barre (px) : le nombre de barres s'adapte à la hauteur

    def __init__(self, name):
        super().__init__()
        self.name, self.value, self.shown, self.peak, self.vmax = name, None, 0.0, 0, 5000
        self.anim = QVariantAnimation(self)
        self.anim.setDuration(450)
        self.anim.valueChanged.connect(self._tick)
        self.setMinimumSize(54, 150)  # la hauteur s'adapte à la catégorie (valeur en haut, nom en bas)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def _tick(self, v):
        self.shown = float(v)
        self.update()

    def set_value(self, v):
        self.value = v
        if v is not None:
            if v > self.vmax:  # l'échelle s'élargit si besoin
                self.vmax = int(-(-v * 1.2 // 1000) * 1000)
            self.peak = max(self.peak, v)
        self.anim.stop()
        self.anim.setStartValue(self.shown)
        self.anim.setEndValue(float(v or 0))
        self.anim.start()
        if v is None:
            tip = f"{self.name} : pas de mesure"
        else:
            etat = ("arrêté ou trop lent" if v < FAN_LOW else "normal" if v <= FAN_GREEN_MAX
                    else "élevé" if v <= FAN_AMBER_MAX else "très élevé")
            tip = f"{self.name} : {v} tr/min ({etat})"
        self.setToolTip(tip)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        txt = self.palette().color(QPalette.WindowText)  # couleur de texte du thème
        w, h, gap, top, bot = self.width(), self.height(), 2, 20, 18
        n = max(self.SEG, int((h - top - bot + gap) // (self.BAR + gap)))  # plus le vumètre est haut, plus il a de barres
        sh = (h - top - bot - gap * (n - 1)) / n
        lit = round(min(1.0, self.shown / self.vmax) * n) if self.vmax else 0
        peak_i = round(min(1.0, self.peak / self.vmax) * n) if (self.value is not None and self.vmax) else 0
        for i in range(n):  # i = 0 en bas
            y = top + (n - 1 - i) * (sh + gap)
            top_rpm = (i + 1) / n * self.vmax  # vitesse atteinte quand ce segment est allumé
            col = (QColor(34, 197, 94) if top_rpm <= FAN_GREEN_MAX
                   else QColor(245, 158, 11) if top_rpm <= FAN_AMBER_MAX else QColor(239, 68, 68))
            if i >= lit:
                col = QColor(txt)
                col.setAlpha(38)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(7, y, w - 14, sh), min(2.5, sh / 2), min(2.5, sh / 2))
            if peak_i - 1 == i and peak_i > lit:  # repère de crête
                pk = QColor(txt)
                pk.setAlpha(170)
                p.setPen(QPen(pk, 1.3))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRectF(7, y, w - 14, sh), min(2.5, sh / 2), min(2.5, sh / 2))
        f = QFont()
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        slow = self.value is not None and self.value < FAN_LOW
        p.setPen(QColor(239, 68, 68) if slow else txt)
        p.drawText(QRectF(0, 0, w, top - 2), Qt.AlignCenter, f"{int(self.value):,}".replace(",", " ") if self.value is not None else "—")
        f.setBold(False)
        f.setPixelSize(10)
        p.setFont(f)
        p.drawText(QRectF(0, h - bot + 2, w, bot - 2), Qt.AlignCenter, self.name)


class DataPanel(QWidget):
    """Données du projecteur en quatre blocs : modèle / heures / erreur · signal · réseau · versions + ventilateurs."""

    def __init__(self):
        super().__init__()
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(10)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(1, 1)
        self.lse = None
        self.lse_val = None
        self.model = QLabel("—")
        self.model.setStyleSheet("font-size:17px;font-weight:700")
        self.model.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f1, v1, self.r1 = self._block("Projecteur")
        v1.addWidget(self.model)
        v1.addLayout(self.r1)
        v1.addStretch()
        f2, v2, self.r2 = self._block("Signal d'entrée")
        v2.addLayout(self.r2)
        v2.addStretch()
        f3, v3, self.r3 = self._block("Réseau")
        v3.addLayout(self.r3)
        v3.addStretch()
        f4, v4, self.r4 = self._block("Versions et ventilateurs")
        row = QHBoxLayout()
        row.setSpacing(8)
        left = QVBoxLayout()  # la liste des versions reste en haut, les vumètres prennent toute la hauteur
        left.setSpacing(0)
        left.addLayout(self.r4)
        left.addStretch()
        row.addLayout(left, 1)
        self.meters = [VuMeter(f"FAN {i}") for i in (1, 2, 3)]
        mrow = QHBoxLayout()
        mrow.setSpacing(0)
        for m in self.meters:
            mrow.addWidget(m)
        row.addLayout(mrow)
        v4.addLayout(row, 1)
        for fr, r, c in ((f1, 0, 0), (f2, 0, 1), (f3, 1, 0), (f4, 1, 1)):
            g.addWidget(fr, r, c)

    @staticmethod
    def _block(title):
        f = QFrame()
        f.setObjectName("pane")
        v = QVBoxLayout(f)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        c = QLabel(title)
        c.setObjectName("cap")
        v.addWidget(c)
        rows = QGridLayout()
        rows.setHorizontalSpacing(12)
        rows.setVerticalSpacing(3)
        rows.setColumnStretch(1, 1)
        return f, v, rows

    @staticmethod
    def _fill(rows, items, empty="—"):
        """Remplit une grille libellé / valeur ; renvoie les étiquettes de valeur."""
        while rows.count():
            w = rows.takeAt(0).widget()
            if w:
                w.deleteLater()
        vals = []
        for i, (k, v) in enumerate(items or [("", empty)]):
            a, b = QLabel(k), QLabel(str(v))
            a.setObjectName("sub")
            for x in (a, b):
                x.setWordWrap(True)
            b.setTextInteractionFlags(Qt.TextSelectableByMouse)
            rows.addWidget(a, i, 0, Qt.AlignTop)
            rows.addWidget(b, i, 1, Qt.AlignTop)
            vals.append(b)
        return vals

    @staticmethod
    def _lse_text(lse):
        if lse is None:
            return "—", False
        n, name = lse
        return ("Aucune", False) if not n else ((LSE_TEXT.get(n) or name or f"code {n}"), True)

    def set_lse(self, lse):
        """Dernière erreur système : rouge si le code est différent de 0."""
        self.lse = lse
        if self.lse_val is not None:
            txt, bad = self._lse_text(lse)
            self.lse_val.setText(txt)
            self.lse_val.setStyleSheet("color:#ef4444;font-weight:700" if bad else "")

    def set_fans(self, rpms):
        for m, v in zip(self.meters, rpms):
            m.set_value(v)

    def set_error(self, msg):
        self.model.setText("—")
        for r in (self.r1, self.r2, self.r3, self.r4):
            self._fill(r, [], "")
        self._fill(self.r1, [("Erreur", msg)])
        self.lse_val = None
        self.set_fans([None, None, None])

    def set_data(self, d):
        self.model.setText(d["model"] or "Modèle non communiqué")
        hours = [(k, pretty(k, v)) for k, v in d["hours"]]
        txt, bad = self._lse_text(d["lse"])
        vals = self._fill(self.r1, hours + [("Dernière erreur système", txt)])
        self.lse_val = vals[-1]
        self.set_lse(d["lse"])
        self._fill(self.r2, d["signal"])
        self._fill(self.r3, d["network"])
        self._fill(self.r4, d["versions"] + d["other"])
        self.set_fans(d["fans"] or [None, None, None])


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


LAMP_DEFAULT = {"cmd": "BRT", "unit": "%", "min": 0, "max": 100, "presets": [25, 50, 75, 100]}
MAX_PRESETS = 8


class LampConfig(QDialog):
    """Réglage de la commande de luminosité (propre à chaque VP)."""

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.setWindowTitle("Luminosité : commande et plage")
        g = QGridLayout(self)
        self.cmd = QLineEdit(cfg["cmd"])
        self.cmd.setMaxLength(3)
        self.unit = QLineEdit(cfg["unit"])
        self.unit.setMaxLength(4)
        self.lo, self.hi = QSpinBox(), QSpinBox()
        for sp, val in ((self.lo, cfg["min"]), (self.hi, cfg["max"])):
            sp.setRange(0, 100000)
            sp.setValue(val)
        for r, (txt, w) in enumerate((("Commande Christie", self.cmd), ("Unité affichée", self.unit),
                                      ("Valeur minimale", self.lo), ("Valeur maximale", self.hi))):
            g.addWidget(QLabel(txt), r, 0)
            g.addWidget(w, r, 1)
        note = QLabel("La valeur est lue par (CMD?) et réglée par (CMD valeur). Luminosité de l'image : BRT "
                      "(0 à 100 %, 50 par défaut) ; LPP règle la puissance de la source lumineuse. Indique la plage acceptée par ton "
                      "projecteur (voir la documentation Christie).")
        note.setObjectName("sub")
        note.setWordWrap(True)
        note.setMinimumWidth(320)
        g.addWidget(note, 4, 0, 1, 2)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        g.addWidget(bb, 5, 0, 1, 2)

    def get(self):
        lo, hi = self.lo.value(), self.hi.value()
        if hi <= lo:
            hi = lo + 1
        return {"cmd": (self.cmd.text().strip().upper() or "BRT")[:3], "unit": self.unit.text().strip(), "min": lo, "max": hi}


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
        self.data_panel, self.prev_pwr, self.last_details = None, None, None
        self.data_dialog = None  # look Mixeur : les données s'affichent dans une fenêtre
        self.again = False  # une actualisation a été demandée pendant une lecture : on la rejoue ensuite
        self.lens_locked, self.lens_btns, self.lens_lock = True, [], None  # boutons d'objectif : verrouillés par défaut
        self.video_folded = False  # automatique : replié quand la source est identifiée, déplié sinon
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.max_rows = {"classic": 99, "console": 3, "tiles": 99, "side": 99, "mixer": 0}.get(look, 99)
        self.ins = [i for i in cfg.get("inputs", list(INPUTS)) if i in INPUTS]
        self.src_key, self.src_label = None, None  # source identifiée par le VP (n° d'entrée) et son affichage
        self.ori, self.ori_btn, self.ori_prev, self.ori_loaded = {"ceiling": None, "rear": None}, {}, None, False
        self.set_folded = self.key in main.cfg.get("settings_folded", [])
        self.set_sum = None
        self.lamp_sl, self.lamp_ready, self.lamp_loaded, self.lamp_last, self.lamp_next = None, False, False, None, None
        self.lamp_timer = QTimer(self)
        self.lamp_timer.setSingleShot(True)
        self.lamp_timer.timeout.connect(lambda: self.lamp_commit())
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
        f = self.reg(g, k, Func(t, lambda: self.go(g, k)))
        if g == "shu":
            f.led.setVisible(False)  # pas de LED : « Ouvrir » passe en bleu, « Fermer » en rouge-orangé quand actif
        return f

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

    def bar(self, full=True, vertical=False, data_cb=None):
        h = QVBoxLayout() if vertical else QHBoxLayout()
        if full:
            b1 = QPushButton("Données")
            b1.clicked.connect(data_cb or self.load_info)
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
        setting = {"data": "folded", "video": "video_folded", "set": "settings_folded"}[kind]

        def is_folded():
            return {"data": self.folded, "video": self.video_folded, "set": self.set_folded}[kind]

        def paint():
            folded = is_folded()
            b.setText(("▸ " if folded else "▾ ") + title)
            body.setVisible(not folded)
            if kind == "video":
                self.update_src_label()
            elif kind == "set":
                self.update_set_sum()

        def toggle():
            folded = not is_folded()
            if kind == "data":
                self.folded = folded
            elif kind == "set":
                self.set_folded = folded
            else:
                self.video_folded = folded
            if kind in ("data", "set"):  # le panneau vidéo, lui, est géré automatiquement (non mémorisé)
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

    def reglages(self, *widgets, framed=False):
        """Groupe « Réglages » repliable : regroupe les paramètres autres que les données."""
        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(16, 0, 0, 0)  # contenu en retrait : on voit ce qui appartient au groupe
        bv.setSpacing(12)
        for w in widgets:
            bv.addWidget(w)
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(self.fold_header("Réglages", body, "set"))
        self.set_sum = QLabel()
        self.set_sum.setObjectName("sub")
        head.addWidget(self.set_sum)
        head.addStretch()
        v.addLayout(head)
        v.addWidget(body)
        self.update_set_sum()
        if framed:
            frame = QFrame()
            frame.setObjectName("pane")
            QVBoxLayout(frame).addWidget(panel)
            return frame
        return panel

    def update_set_sum(self):
        """Groupe replié : rappel de la source active à côté du titre."""
        lab = self.set_sum
        if lab is None:
            return
        txt = INPUTS.get(self.src_key, "") if self.set_folded else ""
        lab.setText(txt)
        lab.setVisible(bool(txt))

    # --- luminosité : curseur + préréglages ---
    def lamp_cfg(self):
        cfg = self.main.cfg
        if not cfg.get("lamp_brt"):  # migration unique : l'ancienne valeur par défaut (LPP) devient BRT
            for v in cfg.get("lamp", {}).values():
                if v.get("cmd") == "LPP":
                    for k in ("cmd", "min", "max", "unit"):
                        v.pop(k, None)
            cfg["lamp_brt"] = True
            self.main.save()
        d = dict(LAMP_DEFAULT)
        d.update(self.main.cfg.get("lamp", {}).get(self.key, {}))
        return d

    def lamp_store(self, **kw):
        store = self.main.cfg.setdefault("lamp", {})
        store.setdefault(self.key, {}).update(kw)
        self.main.save()

    def lamp_panel(self, cap="Luminosité", framed=False, compact=False, width=0):
        c = self.lamp_cfg()
        self.lamp_cols = 3 if compact else 5
        w = QWidget()
        if width:
            w.setFixedWidth(width)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(self.cap(cap))
        head.addStretch()
        self.lamp_val = QLabel("—")
        self.lamp_val.setStyleSheet("font-weight:600")
        head.addWidget(self.lamp_val)
        gear = QPushButton("⚙")
        gear.setFixedWidth(28)
        gear.setToolTip("Commande et plage de la luminosité")
        gear.clicked.connect(self.lamp_config)
        head.addWidget(gear)
        v.addLayout(head)
        sl = QSlider(Qt.Horizontal)
        sl.setRange(c["min"], c["max"])
        sl.setCursor(Qt.PointingHandCursor)
        sl.valueChanged.connect(self.lamp_changed)
        sl.sliderReleased.connect(lambda: self.lamp_commit())  # envoi dès que le curseur est relâché
        self.lamp_sl = sl
        v.addWidget(sl)
        self.lamp_grid = QGridLayout()
        self.lamp_grid.setSpacing(6)
        v.addLayout(self.lamp_grid)
        self.lamp_rebuild()
        if framed:
            frame = QFrame()
            frame.setObjectName("pane")
            QVBoxLayout(frame).addWidget(w)
            return frame
        return w

    def lamp_rebuild(self):
        c = self.lamp_cfg()
        while self.lamp_grid.count():
            it = self.lamp_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        cur = self.lamp_sl.value()
        for i, p in enumerate(sorted(set(c["presets"]))):
            b = QPushButton(f"{p} {c['unit']}".strip())
            b.setStyleSheet("padding:3px 8px;font-size:12px")
            b.setCursor(Qt.PointingHandCursor)
            b.setProperty("st", "ok" if p == cur and self.lamp_ready else "off")
            b.setToolTip("Clic : régler cette luminosité · clic droit ou bouton − : supprimer ce préréglage")
            b.clicked.connect(lambda _=False, val=p: self.lamp_preset(val))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pos, val=p, btn=b: self.lamp_chip_menu(btn, pos, val))
            self.lamp_grid.addWidget(b, i // self.lamp_cols, i % self.lamp_cols)
        n = len(set(c["presets"]))
        add = QPushButton("+")
        add.setStyleSheet("padding:3px 8px;font-size:12px")
        add.setToolTip("Ajouter la valeur actuelle du curseur comme préréglage")
        add.setEnabled(n < MAX_PRESETS)
        add.clicked.connect(self.lamp_add_preset)
        self.lamp_grid.addWidget(add, n // self.lamp_cols, n % self.lamp_cols)
        rm = QPushButton("−")
        rm.setStyleSheet("padding:3px 8px;font-size:12px")
        rm.setToolTip("Supprimer le préréglage correspondant à la valeur actuelle du curseur")
        rm.setEnabled(cur in c["presets"])
        rm.clicked.connect(lambda: self.lamp_del_preset(self.lamp_sl.value()))
        self.lamp_rm = rm
        self.lamp_grid.addWidget(rm, (n + 1) // self.lamp_cols, (n + 1) % self.lamp_cols)

    def lamp_chip_menu(self, btn, pos, val):
        m = QMenu(btn)
        act = m.addAction(f"Supprimer le préréglage {val}")
        if m.exec(btn.mapToGlobal(pos)) is act:
            self.lamp_del_preset(val)

    def lamp_del_preset(self, val):
        c = self.lamp_cfg()
        if val in c["presets"]:
            self.lamp_store(presets=[p for p in c["presets"] if p != val])
            self.lamp_rebuild()
            self.status(f"{self.cfg['name']} · préréglage {val} {c['unit']} supprimé")

    def lamp_add_preset(self):
        c = self.lamp_cfg()
        v = self.lamp_sl.value()
        if v not in c["presets"] and len(c["presets"]) < MAX_PRESETS:
            self.lamp_store(presets=sorted(set(c["presets"]) | {v}))
            self.lamp_rebuild()
            self.status(f"{self.cfg['name']} · préréglage {v} {c['unit']} ajouté")

    def lamp_preset(self, val):
        self.lamp_sl.setValue(val)
        self.lamp_commit()

    def lamp_config(self):
        d = LampConfig(self, self.lamp_cfg())
        if d.exec():
            new = d.get()
            self.lamp_store(**new)
            self.lamp_sl.setRange(new["min"], new["max"])
            self.lamp_loaded = self.lamp_ready = False
            self.lamp_val.setText("—")
            self.lamp_rebuild()
            bg(self.read_lamp, self.apply_lamp)

    def lamp_changed(self, v):
        self.lamp_val.setText(f"{v} {self.lamp_cfg()['unit']}".strip())
        if self.lamp_ready:
            self.lamp_rebuild_marks()
        if not self.lamp_sl.isSliderDown() and self.lamp_ready:  # clic sur la rainure, clavier, molette
            self.lamp_timer.start(400)

    def lamp_rebuild_marks(self):
        v = self.lamp_sl.value()
        rm = getattr(self, "lamp_rm", None)
        if rm is not None:
            rm.setEnabled(v in self.lamp_cfg()["presets"])
        for i in range(self.lamp_grid.count()):
            b = self.lamp_grid.itemAt(i).widget()
            if b is not None and b.text() != "+":
                try:
                    p = int(b.text().split()[0])
                except ValueError:
                    continue
                b.setProperty("st", "ok" if p == v else "off")
                repolish(b)

    def lamp_commit(self):
        self.lamp_timer.stop()
        v = self.lamp_sl.value()
        c = self.lamp_cfg()
        if self.main.locked:
            self.status("Interface verrouillée : déverrouille pour envoyer une commande")
            if self.lamp_last is not None:
                self.lamp_set_silent(self.lamp_last)
            return
        if v == self.lamp_last:
            return
        if "lamp" in self.pending:  # un envoi est en cours : on enverra la dernière valeur ensuite
            self.lamp_next = v
            return
        self.pending.add("lamp")
        cmd = f"{c['cmd']} {v}"
        tol = max(1, round((c["max"] - c["min"]) * 0.01))

        def work():
            msgs = self.p.ask(cmd, quiet=True)
            bad = [m for m in msgs if "ERR" in m]
            if bad:
                raise RuntimeError("refusé par le projecteur")
            t_end = time.time() + 6
            while time.time() < t_end:
                cur = self.read_lamp()
                if cur is not None and abs(cur - v) <= tol:
                    return cur
                time.sleep(0.6)
            return None

        def done(res, err):
            self.pending.discard("lamp")
            good = res is not None and not err
            unit = c["unit"]
            if good:
                self.lamp_last, self.lamp_ready = res, True
                self.lamp_set_silent(res)
            self.status(f"{self.cfg['name']} · luminosité {v} {unit} : "
                        + ("confirmée" if good else f"non confirmée ({err or 'pas de réponse du VP'})"))
            nxt, self.lamp_next = self.lamp_next, None
            if nxt is not None and nxt != self.lamp_last:
                self.lamp_set_silent(nxt)
                self.lamp_commit()
        bg(self.urgent(work), done)

    def lamp_set_silent(self, v):
        """Positionne le curseur sans déclencher d'envoi."""
        sl = self.lamp_sl
        sl.blockSignals(True)
        sl.setRange(min(sl.minimum(), v), max(sl.maximum(), v))
        sl.setValue(v)
        sl.blockSignals(False)
        self.lamp_val.setText(f"{v} {self.lamp_cfg()['unit']}".strip())
        self.lamp_rebuild_marks()

    def read_lamp(self):
        """Lit la luminosité courante par (CMD?) : entier, ou None si le VP ne répond pas."""
        code = self.lamp_cfg()["cmd"]
        for m in self.p.ask(f"{code}?", timeout=3):
            mm = re.search(rf"{re.escape(code)}!\s*(-?\d+)", m, re.I)
            if mm:
                return int(mm[1])
        return None

    def apply_lamp(self, v, err):
        if "lamp" in self.pending or self.lamp_sl is None or self.lamp_sl.isSliderDown():
            return
        if err or v is None:
            self.lamp_sl.setToolTip("Luminosité non lue : vérifie la commande avec ⚙")
            return
        self.lamp_sl.setToolTip("")
        self.lamp_ready, self.lamp_last = True, v
        self.lamp_set_silent(v)

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
        self.data_panel = DataPanel()
        l.addWidget(self.data_panel)
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
        v.addWidget(self.reglages(self.lamp_panel(), self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], cols=3), self.ori_panel()))
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
        h.addStretch()
        h.addLayout(self.bar(vertical=True))
        v.addLayout(h)
        v.addWidget(self.reglages(self.lamp_panel(width=420), self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], cols=3), self.ori_panel(compact=True)))
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
        v.addWidget(self.reglages(self.lamp_panel(), self.video_panel([self.S("src", i, INPUTS[i]) for i in self.ins], cols=3),
                                  self.ori_panel()))
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
        v.addLayout(r)
        v.addWidget(self.reglages(self.lamp_panel(), self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], cols=3),
                                  self.ori_panel(), framed=True))
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
        v.addWidget(self.reglages(self.lamp_panel(compact=True), self.ori_panel(compact=True, vertical=True),
                                  self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], vertical=True)))
        v.addStretch()
        self.lcd = QLabel("—")
        self.lcd.setObjectName("lcd")
        self.lcd.setAlignment(Qt.AlignCenter)
        v.addWidget(self.lcd)
        self.data_dialog = QDialog(self)  # le Mixeur est trop étroit pour le panneau : il s'ouvre à part
        self.data_dialog.setWindowTitle(f"Données — {self.cfg['name']}")
        self.data_dialog.resize(820, 540)
        dl = QVBoxLayout(self.data_dialog)
        dl.setContentsMargins(16, 16, 16, 16)
        self.data_panel = DataPanel()
        dl.addWidget(self.data_panel)
        dl.addStretch()
        v.addLayout(self.bar(data_cb=self.open_data))

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
        extra += slider_qss(c)
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
QPushButton#pwr[st="down"] {{ border-color: #ef4444; }}
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

        video = self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], cols=4)
        v.addWidget(self.reglages(self.lamp_panel("LUMINOSITÉ"), video, self.ori_panel(), framed=True))
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
        v.addWidget(self.reglages(self.lamp_panel("LUMINOSITÉ"), self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], vertical=False),
                                  self.ori_panel(), framed=True))
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
        rv.addWidget(self.reglages(self.lamp_panel("LUMINOSITÉ"), self.video_panel([self.F("src", i, INPUTS[i]) for i in self.ins], cols=2),
                                   self.ori_panel()))
        rv.addStretch()
        body.addWidget(right, 2)
        v.addLayout(body, 2)

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
        iv.addWidget(self.reglages(self.lamp_panel("LUMINOSITÉ"), self.video_panel([self.S("src", i, INPUTS[i]) for i in self.ins], cols=3),
                                   self.ori_panel()))
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
        srcv.addWidget(self.reglages(self.lamp_panel("LUMINOSITÉ"), self.video_panel([self.S("src", i, INPUTS[i]) for i in self.ins], cols=3),
                                     self.ori_panel()))
        v.addWidget(source)
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
        red = {"shu": "closed", "pwr": "off"}.get(group)  # bouton rouge quand cet état est confirmé
        for k, f in self.groups[group].items():
            st = state if k == key else "off"
            w = getattr(f, "btn", f)  # Func : le bouton est f.btn ; Seg : f lui-même ; Ring (bouton rond) : aucun
            tint = hasattr(w, "setProperty")
            if tint and k == red:
                w.setProperty("shut", "1")  # couleur définie par le thème (themes.py / skin)
            f.led.set(st)
            # Func : le bouton n'a pas de propriété d'état par défaut : « Fermer » / « Éteindre » deviennent rouges,
            # « Ouvrir » / « Allumer » prennent la couleur d'accent.
            if group in ("shu", "pwr") and tint and w is not f:
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
        self.update_set_sum()

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
        """Réglages mécaniques : aperçu, position du projecteur (plafond / rétro, l'un au-dessus de l'autre)
        et, à droite, l'objectif (calibrage, centrer le shift, verrou)."""
        size = 28 if compact else 40
        self.ori_prev = OriPreview(compact)
        b1 = OrientBtn("Au plafond\nImage à l'envers", ceiling_svg, lambda: self.toggle_ori("ceiling"), size)
        b2 = OrientBtn("En rétro\nImage en miroir", rear_svg, lambda: self.toggle_ori("rear"), size)
        b1.setToolTip("Montage au plafond : retourne l'image de haut en bas (commande CEL)")
        b2.setToolTip("Rétroprojection : inverse l'image de gauche à droite (commande SOR)")
        self.ori_btn = {"ceiling": b1, "rear": b2}
        self.lockables += [b1, b2]
        bc = OrientBtn("Calibrage objectif", lens_svg, lambda: self.lens("LCB+HOME 1", bc, "Calibrage de l'objectif"), size)
        bs = OrientBtn("Centrer Shift", center_svg, lambda: self.lens("LCB+HOME 0", bs, "Centrage du shift"), size)
        bc.setToolTip("Calibrage de l'objectif (commande LCB+HOME 1)")
        bs.setToolTip("Centrer le décalage (shift) de l'objectif (commande LCB+HOME 0)")
        lk = QPushButton()
        lk.setFixedSize(36, 36)
        lk.setIconSize(QSize(20, 20))
        lk.setCursor(Qt.PointingHandCursor)
        lk.clicked.connect(self.toggle_lens_lock)
        self.lens_btns, self.lens_lock = [bc, bs], lk
        self.lockables.append(lk)

        tip = ("Aperçu (pas un bouton) : la lettre F montre comment l'image sera affichée avec les réglages "
               "de position. Le point vert marque le coin haut-gauche de l'image d'origine.")
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

        def block(title):
            """Catégorie encadrée avec son titre, comme « Réseau » ou « Signal d'entrée »."""
            fr = QFrame()
            fr.setObjectName("pane")
            lay = QVBoxLayout(fr)
            m = 8 if vertical else 12
            lay.setContentsMargins(m, 10, m, 10)
            lay.setSpacing(8)
            lay.addWidget(self.cap(title))
            return fr, lay

        f1, l1 = block("Position du projecteur")
        btns1 = QVBoxLayout()
        btns1.setSpacing(8)
        btns1.addWidget(b1)
        btns1.addWidget(b2)
        if vertical:
            l1.addWidget(pv, 0, Qt.AlignHCenter)
            l1.addLayout(btns1)
        else:
            row1 = QHBoxLayout()
            row1.setSpacing(14)
            row1.addWidget(pv, 0, Qt.AlignVCenter)
            row1.addLayout(btns1, 1)
            l1.addLayout(row1)

        f2, l2 = block("Objectif")  # deux boutons + petit bouton de verrouillage
        row = QHBoxLayout()
        row.setSpacing(8)
        btns = QVBoxLayout()
        btns.setSpacing(8)
        btns.addWidget(bc)
        btns.addWidget(bs)
        row.addLayout(btns, 1)
        row.addWidget(lk, 0, Qt.AlignVCenter)
        l2.addLayout(row)

        body = QWidget()
        if vertical:
            l = QVBoxLayout(body)
            l.setContentsMargins(0, 0, 0, 0)
            l.setSpacing(22)  # grand espace entre les deux catégories
            l.addWidget(f1)
            l.addWidget(f2)
        else:
            l = QHBoxLayout(body)
            l.setContentsMargins(0, 0, 0, 0)
            l.setSpacing(28)  # grand espace entre les deux catégories
            l.addWidget(f1, 3)
            l.addWidget(f2, 2)
            for b in (b1, b2, bc, bs):
                b.setMaximumWidth(250)
            l.addStretch(1)
        self.apply_lens_lock()
        self.paint_ori()
        if framed:
            frame = QFrame()
            frame.setObjectName("pane")
            QVBoxLayout(frame).addWidget(body)
            return frame
        return body

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
        bg(self.urgent(lambda: self.p.set_and_check(cmd, code, 1 if target else 0)), done)

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
            msgs = self.p.ask(cmd, timeout=1.5, idle=0.3)  # 1) on envoie et on lit la réponse du VP
            bad = [m for m in msgs if "ERR" in m]
            if bad:                                # refus explicite du VP : inutile d'attendre la fin du délai
                txt = re.search(r'"([^"]*)"', bad[0])
                raise RuntimeError("refusé par le projecteur" + (f" ({txt[1]})" if txt else ""))
            if reply_state(msgs, group) == key:    # 2) la réponse annonce déjà le nouvel état : couleur tout de suite
                return True
            return wait_for(getter, key, timeout, every=1.5 if group == "pwr" else 0.6)  # 3) sinon, relecture

        def done(ok, err):
            self.pending.discard(group)
            if group == "pwr":
                self.pwr_stop()
            good = bool(ok) and not err
            self.led(group, key, "ok" if good else "err")
            if group == "pwr" and good:
                if key == "off":   # éteint : plus aucune couleur sur le shutter ni sur la source
                    self.led("shu", None, "ok")
                    self.led("src", None, "ok")
                    self.sync_video(None)
                    self.mark_off()
                else:              # allumé : on relit tout (état, luminosité, position, données)
                    self.refresh(True)
            if group == "src" and good:
                self.src_key = key
                self.set_video_folded(True)
            self.status(f"{self.cfg['name']} · {cmd} : " + ("confirmé" if good else f"non confirmé ({err or 'délai dépassé'})"))
        bg(self.urgent(work), done)

    def refresh(self, full=False):
        if full:
            self.ori_loaded = False  # relire aussi la position du VP
            self.load_info()         # et les données
        if self.busy:
            self.again = True
            return
        self.busy = True
        bg(self.p.state, self.apply)

    def apply(self, st, err):
        self.busy = False
        if self.again:
            self.again = False
            QTimer.singleShot(0, self.refresh)
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
        on = (not err) and st["pwr"] == "on"
        for g in self.groups:
            if g in self.pending:
                continue
            val = None if err else st[g]
            if g in ("shu", "src") and not on:
                val = None  # éteint, en veille ou en transition : shutter et source sans couleur
            self.led(g, val, "ok")
        if "src" not in self.pending:
            self.sync_video(st.get("src") if on else None)
        self.on_signal(st.get("sig") if on else None)
        pw = None if err else st["pwr"]
        if pw == "on" and self.prev_pwr not in (None, "on"):  # le VP vient de s'allumer (appli ou télécommande)
            self.ori_loaded = False
            self.load_info()
        if pw is not None:
            self.prev_pwr = pw
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
        if self.lamp_sl is not None and "lamp" not in self.pending:
            if on:  # luminosité (BRT) lue dès que le VP est allumé, puis à chaque actualisation
                self.lamp_sl.setEnabled(True)
                bg(self.read_lamp, self.apply_lamp)
            else:
                self.mark_off()
        if self.data_panel is not None and self.data_visible():  # vumètres des ventilateurs
            if on:
                bg(self.p.fans, self.apply_fans)
            else:
                self.mark_off()
        elif on and self.poll_n % 3 == 0:  # panneau masqué : on surveille quand même les ventilateurs (alertes)
            bg(self.p.fans, self.apply_fans)
        if not on:
            self.fan_check(None)  # éteint, en transition ou hors ligne : les vitesses ne veulent plus rien dire
        self.update_state_lbl()

    # --- échanges d'autres logiciels vus sur le réseau (voir sniffer.py) ---
    SET_CMD = re.compile(r"^#?([A-Za-z]{3})(?:\+[A-Za-z0-9]{4})?(?![A-Za-z0-9+])\s*[^?\s]")

    def on_remote(self, from_vp, msgs):
        """Messages échangés entre le VP et un autre logiciel. Une réponse du VP (ex. (SHU!001)) met l'état à jour
        tout de suite ; un ordre d'un autre logiciel déclenche en plus une relecture complète quelques instants après."""
        if self.last_st is None:  # VP encore inconnu ou hors ligne : la prochaine actualisation s'en charge
            return
        recheck = False
        if from_vp:
            st, pwr_changed, changed = dict(self.last_st), False, False
            for g in ("pwr", "shu", "src"):
                v = reply_state(msgs, g)
                if v is not None and v != st.get(g):
                    st[g], changed = v, True
                    pwr_changed = pwr_changed or g == "pwr"
            if changed:
                self.apply_remote_state(st)
                recheck = True
        else:
            recheck = any(self.SET_CMD.match(m) for m in msgs)  # ordre (pas une simple requête « ? »)
        if recheck:
            self.remote_refresh(full=any(re.match(r"^#?(CEL|SOR|BRT|LPP|LPM|PWR)\b", m, re.I) for m in msgs))

    def apply_remote_state(self, st):
        """Même rendu que apply() pour les boutons, mais à partir d'une réponse vue sur le réseau."""
        self.last_st = st
        on = st["pwr"] == "on"
        for g in self.groups:
            if g in self.pending:
                continue
            val = st.get(g)
            if g in ("shu", "src") and not on:
                val = None
            self.led(g, val, "ok")
        if "src" not in self.pending:
            self.sync_video(st.get("src") if on else None)
        if not on:
            self.on_signal(None)
            self.mark_off()
        self.update_state_lbl()
        name = self.cfg["name"]
        self.status(f"{name} : changement fait par un autre logiciel (état mis à jour)")

    def remote_refresh(self, full=False):
        """Relecture complète peu après un ordre venu d'ailleurs (regroupe les rafales d'ordres)."""
        t = getattr(self, "remote_timer", None)
        if t is None:
            t = self.remote_timer = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._remote_refresh_now)
        self.remote_full = getattr(self, "remote_full", False) or full
        t.start(400)

    def _remote_refresh_now(self):
        full, self.remote_full = self.remote_full, False
        if full:
            self.ori_loaded = False  # position (plafond / rétro) et luminosité sont relues avec l'état
        self.refresh()

    def urgent(self, fn):
        """Exécute fn en priorité sur les lectures de fond (le VP ne traite qu'une conversation à la fois)."""
        def run():
            with self.p.priority():
                return fn()
        return run

    def mark_off(self):
        """VP éteint, en transition ou injoignable : luminosité grisée, ventilateurs à l'arrêt."""
        if self.lamp_sl is not None and "lamp" not in self.pending:
            self.lamp_sl.setEnabled(False)
            self.lamp_ready = False
            self.lamp_val.setText("—")
            self.lamp_sl.setToolTip("Le projecteur doit être allumé pour régler la luminosité")
        if self.data_panel is not None:
            self.data_panel.set_fans([None, None, None])

    def apply_fans(self, fans, err):
        if self.data_panel is not None:
            self.data_panel.set_fans([None, None, None] if err else fans)
        on = self.last_st is not None and self.last_st.get("pwr") == "on"
        self.fan_check(fans if (on and not err) else None)

    FAN_WORDS = {"slow": ("err", "trop lent ou arrêté"), "fast": ("err", "trop rapide"), "high": ("warn", "à une vitesse élevée")}

    def fan_check(self, fans):
        """Alertes ventilateurs (seuils FAN_LOW / FAN_GREEN_MAX / FAN_AMBER_MAX). fans = [tr/min | None] x 3, ou None
        quand les vitesses ne sont pas exploitables. Une alerte n'apparaît qu'après deux relevés consécutifs identiques
        (pas de fausse alerte sur une mesure isolée) et disparaît dès que la vitesse redevient normale."""
        counts = getattr(self, "fan_counts", None)
        if counts is None:
            counts = self.fan_counts = {}
        for i in range(3):
            v = fans[i] if fans else None
            state = (None if v is None else "slow" if v < FAN_LOW else "fast" if v > FAN_AMBER_MAX
                     else "high" if v > FAN_GREEN_MAX else None)
            key = f"fan{i + 1}:" + self.key
            if state is None:
                counts.pop(i, None)
                self.main.clear_alert(key)
                continue
            prev, n = counts.get(i, (None, 0))
            n = n + 1 if prev == state else 1
            counts[i] = (state, n)
            if n >= 2:
                level, word = self.FAN_WORDS[state]
                self.main.set_alert(key, level, f"{self.cfg['name']} : ventilateur {i + 1} {word}")

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
        if self.data_panel is not None:
            self.data_panel.set_lse((n, name))
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
        self.apply_lens_lock()

    def toggle_lens_lock(self):
        self.lens_locked = not self.lens_locked
        self.apply_lens_lock()

    def apply_lens_lock(self):
        for b in self.lens_btns:
            b.setEnabled(not self.main.locked)  # verrou global : désactivés ; verrou d'objectif : atténués + explication au clic
            eff = getattr(b, "eff", None)
            if eff is None:
                eff = b.eff = QGraphicsOpacityEffect(b)
                eff.setOpacity(0.45)
                b.setGraphicsEffect(eff)
            eff.setEnabled(self.lens_locked)
        lk = self.lens_lock
        if lk is not None:
            lk.setIcon(svg_icon(lock_svg("#8b93a7" if self.lens_locked else "#f59e0b", self.lens_locked)))
            lk.setProperty("st", "off" if self.lens_locked else "busy")
            repolish(lk)
            lk.setToolTip("Verrouillé : clique pour déverrouiller les boutons de l'objectif" if self.lens_locked
                          else "Déverrouillé : clique pour reverrouiller")

    @staticmethod
    def _reset_btn(btn):
        try:
            btn.set("off")
        except RuntimeError:  # bouton supprimé entre-temps
            pass

    def lens(self, cmd, btn, label):
        """Commandes d'objectif (LCB+HOME 1 : calibrage, LCB+HOME 0 : centrer le shift)."""
        if self.main.locked:
            self.status("Interface verrouillée : déverrouille pour envoyer une commande")
            return
        if self.lens_locked:  # on explique au lieu de ne rien faire
            self.status(f"{self.cfg['name']} · boutons d'objectif verrouillés : clique d'abord sur le cadenas")
            self.lens_lock.setProperty("st", "err")
            repolish(self.lens_lock)
            QTimer.singleShot(900, self.apply_lens_lock)
            return
        btn.set("busy")
        self.status(f"{self.cfg['name']} · {label} : envoi de {cmd}…")

        def done(msgs, err):
            bad = [m for m in (msgs or []) if "ERR" in m]
            good = not err and not bad
            btn.set("off" if good else "err")
            self.status(f"{self.cfg['name']} · {label} ({cmd}) : " + ("commande envoyée" if good else f"échec ({err or bad[0]})"))
            if not good:
                QTimer.singleShot(3000, lambda: self._reset_btn(btn))
        bg(self.urgent(lambda: self.p.ask(cmd, timeout=1.5, idle=0.4)), done)

    def data_visible(self):
        """Le panneau de données est-il à l'écran ? (sinon on n'interroge pas les ventilateurs pour rien)"""
        if self.data_dialog is not None:
            return self.data_dialog.isVisible()
        return not self.folded

    def open_data(self):
        """Look Mixeur : ouvre la fenêtre des données (modèle, heures, signal, réseau, versions, ventilateurs)."""
        dlg = self.data_dialog
        if dlg is None:
            return
        if self.last_details is not None:
            self.data_panel.set_data(self.last_details)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self.load_info()
        if self.last_st and self.last_st.get("pwr") == "on":
            bg(self.p.fans, self.apply_fans)

    def load_info(self):
        bg(self.p.details, self.show_details)

    def show_details(self, d, err):
        """Affiche les données rangées par catégories (panneau, bandeau de la Console ou afficheur du Mixeur)."""
        self.last_details = d
        if err or d is None:
            msg = str(err) if err else "aucune donnée"
            if self.strip is not None:
                self.strip.setText(f"Données : {msg}")
            if self.lcd is not None:
                self.lcd.setText("—")
            if self.data_panel is not None:
                self.data_panel.set_error(msg)
            return
        hours = [(k, pretty(k, v)) for k, v in d["hours"]]
        if self.strip is not None:  # look Console : une ligne
            parts = ([d["model"]] if d["model"] else []) + [f"{k} : {v}" for k, v in hours]
            lse = d["lse"]
            if lse and lse[0]:
                parts.append("Erreur : " + (LSE_TEXT.get(lse[0]) or lse[1] or f"code {lse[0]}"))
            self.strip.setText("   ·   ".join(parts) or "Aucune donnée disponible")
        if self.lcd is not None:  # look Mixeur : un afficheur (1re valeur, détail dans l'infobulle)
            self.lcd.setText(hours[0][1][:16] if hours else "—")
            allrows = [("Modèle", d["model"])] + hours + d["signal"] + d["network"] + d["versions"] + d["other"]
            self.lcd.setToolTip("\n".join(f"{k} : {v}" for k, v in allrows if v))
        if self.data_panel is not None:
            self.data_panel.set_data(d)


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
        self.listen = QCheckBox("Écouter le réseau : suivre en direct les ordres envoyés au projecteur par d'autres logiciels "
                                "(demande scapy + Npcap sous Windows, droits administrateur)")
        self.listen.setChecked(cfg.get("listen", True))
        v.addWidget(self.listen)
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
        self.setWindowTitle("Christie Control · D20HD-HS" + (f"  —  RÉGLAGES DE TEST : {CFG.name}" if os.environ.get("CHRISTIE_CONFIG") else ""))
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
        self.tx_led = ActivityLed("#3b82f6", "Envoi : l'appli envoie une commande à un projecteur")
        self.rx_led = ActivityLed("#22c55e", "Réception : des données arrivent d'un projecteur")
        self.listen_lbl = QLabel()
        self.listen_lbl.setObjectName("sub")
        top.addWidget(self.listen_lbl)
        top.addSpacing(6)
        for led, name in ((self.tx_led, "Env."), (self.rx_led, "Réc.")):
            lb = QLabel(name)
            lb.setObjectName("sub")
            top.addWidget(led)
            top.addWidget(lb)
        top.addSpacing(10)
        # branchement des voyants : envois / réceptions de l'appli (protocol.py) et échanges vus sur le réseau
        protocol.ACTIVITY["tx"] = lambda: relay.call.emit(self.tx_led.flash)
        protocol.ACTIVITY["rx"] = lambda: relay.call.emit(self.rx_led.flash)
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
        self.msg = QLabel(f"Prêt · réglages : {CFG}")
        self.msg.setObjectName("sub")
        v.addWidget(self.msg)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: (self.sync_net(), [c.refresh() for c in self.cards]))
        self.set_theme(self.theme.currentText(), save=False)
        self.build()
        if LOAD_WARNING:
            QTimer.singleShot(400, self.show_load_warning)
        if sys.platform == "win32" and self.cfg.get("listen", True) and self.cfg.get("npcap_ask", True) and not npcap_installed():
            QTimer.singleShot(900, self.offer_npcap)  # après l'affichage de la fenêtre

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
        self.start_listener()

    # --- écoute du réseau : ordres envoyés au VP par d'autres logiciels ---
    def set_listen_status(self, ok, text=""):
        if not self.cfg.get("listen", True):
            self.listen_lbl.setText("Écoute réseau : désactivée")
            self.listen_lbl.setToolTip("Réglable dans Paramètres")
        elif ok is None:
            self.listen_lbl.setText("Écoute réseau : démarrage…")
        elif ok:
            self.listen_lbl.setText('<span style="color:#22c55e">●</span> Écoute réseau')
            self.listen_lbl.setToolTip(text)
        else:
            self.listen_lbl.setText('<span style="color:#f59e0b">●</span> Écoute réseau indisponible')
            self.listen_lbl.setToolTip(text + "\n\nL'appli continue de suivre le projecteur par actualisation automatique.")
            self.msg.setText("Écoute réseau indisponible : " + text)

    def start_listener(self):
        old = getattr(self, "listener", None)
        if old is not None:
            old.stop()
        self.listener = None
        if not self.cfg.get("listen", True) or not self.cards:
            self.set_listen_status(None)
            return
        by_addr = {(c.cfg["ip"], int(c.cfg.get("port", PORT))): c for c in self.cards}
        cards = list(self.cards)

        def is_own(client):
            return any(client in c.p.own for c in cards)

        def on_msgs(addr, from_vp, msgs):
            relay.call.emit((self.rx_led if from_vp else self.tx_led).flash)
            card = by_addr.get(addr)
            if card is not None:
                relay.call.emit(lambda: card.on_remote(from_vp, msgs))

        def on_status(ok, text):
            relay.call.emit(lambda: self.set_listen_status(ok, text))
        self.set_listen_status(None)
        self.listener = NetListener(by_addr, is_own, on_msgs, on_status)
        self.listener.start()

    def closeEvent(self, e):
        if getattr(self, "listener", None) is not None:
            self.listener.stop()
        super().closeEvent(e)

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

    def offer_npcap(self):
        """Windows : l'écoute réseau a besoin de Npcap. S'il manque, on propose de l'installer."""
        box = QMessageBox(QMessageBox.Question, "Npcap n'est pas installé",
                          "L'écoute du réseau (suivre en direct les ordres envoyés au projecteur par d'autres logiciels) "
                          "a besoin de Npcap, qui n'est pas installé sur cet ordinateur.\n\n"
                          "Ouvrir la page de téléchargement de Npcap ? Lance l'installeur, puis relance l'appli.\n"
                          "(Npcap est gratuit pour un usage personnel ; voir sa licence pour un usage en entreprise.)\n\n"
                          "Sans Npcap, l'appli fonctionne normalement mais ne voit que ses propres échanges.",
                          QMessageBox.NoButton, self)
        yes = box.addButton("Installer Npcap", QMessageBox.AcceptRole)
        box.addButton("Plus tard", QMessageBox.RejectRole)
        never = box.addButton("Ne plus demander", QMessageBox.DestructiveRole)
        box.exec()
        if box.clickedButton() is yes:
            webbrowser.open(NPCAP_URL)
            self.msg.setText("Installe Npcap (laisse « WinPcap API-compatible Mode » coché), puis relance l'appli")
        elif box.clickedButton() is never:
            self.cfg["npcap_ask"] = False
            self.save()

    def show_load_warning(self):
        box = QMessageBox(QMessageBox.Warning, "Réglages", LOAD_WARNING, QMessageBox.Ok, self)
        box.open()

    def settings(self):
        d = Settings(self.cfg, self)
        if d.exec():
            self.cfg["projectors"], self.cfg["poll"] = d.projectors(), d.poll.value()
            self.cfg["iface"] = d.iface.currentData()
            self.cfg["confirm_off"], self.cfg["stagger"] = d.confirm.isChecked(), d.stagger.value()
            self.cfg["listen"] = d.listen.isChecked()
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
