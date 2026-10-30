import json, re, sys, threading
from pathlib import Path
from PySide6.QtCore import QByteArray, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QPlainTextEdit,
                               QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem,
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


class Card(QFrame):
    def __init__(self, cfg, status, main):
        super().__init__()
        self.setObjectName("card")
        self.cfg, self.status, self.main = cfg, status, main
        self.p = Projector(cfg["ip"], cfg.get("port", PORT))
        self.groups, self.pending, self.busy, self.info_loaded = {}, set(), False, False
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(10)
        head = QHBoxLayout()
        self.dot = Led()
        name = QLabel(cfg["name"])
        name.setObjectName("title")
        ip = QLabel(f'{cfg["ip"]}:{cfg.get("port", PORT)}')
        ip.setObjectName("sub")
        head.addWidget(self.dot)
        head.addWidget(name)
        head.addStretch()
        head.addWidget(ip)
        v.addLayout(head)

        v.addWidget(self.cap("Alimentation"))
        v.addLayout(self.row([self.mk("pwr", "on", "Allumer", "PWR 1", self.p.power, 150),
                              self.mk("pwr", "off", "Éteindre", "PWR 0", self.p.power, 240)]))
        v.addWidget(self.cap("Shutter"))
        v.addLayout(self.row([self.mk("shu", "open", "Ouvrir", "SHU 0", self.p.shutter, 8),
                              self.mk("shu", "closed", "Fermer", "SHU 1", self.p.shutter, 8)]))
        v.addWidget(self.cap("Entrée vidéo"))
        ins = [i for i in cfg.get("inputs", list(INPUTS)) if i in INPUTS]
        g = QGridLayout()
        g.setSpacing(8)
        for n, i in enumerate(ins):
            g.addWidget(self.mk("src", i, INPUTS[i], f"SIN+MAIN {i}", self.p.source, 10), n // 3, n % 3)
        v.addLayout(g)

        v.addWidget(self.cap("Données du projecteur"))
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(14)
        v.addLayout(self.grid)
        bar = QHBoxLayout()
        b1, b2 = QPushButton("Actualiser les données"), QPushButton("Console")
        b1.clicked.connect(self.load_info)
        b2.clicked.connect(lambda: Console(self.p, cfg["name"], self.main).show())
        bar.addWidget(b1)
        bar.addWidget(b2)
        v.addLayout(bar)
        v.addStretch()

    def cap(self, t):
        l = QLabel(t)
        l.setObjectName("cap")
        return l

    def row(self, funcs):
        h = QHBoxLayout()
        h.setSpacing(12)
        for f in funcs:
            h.addWidget(f)
        return h

    def mk(self, group, key, text, cmd, getter, timeout):
        f = Func(text, lambda: self.act(group, key, cmd, getter, timeout))
        self.groups.setdefault(group, {})[key] = f
        return f

    def led(self, group, key, state):
        for k, f in self.groups[group].items():
            f.led.set(state if k == key else "off")

    def act(self, group, key, cmd, getter, timeout):
        if group in self.pending:
            return
        self.pending.add(group)
        self.led(group, key, "busy")

        def work():
            self.p.ask(cmd, quiet=True)           # 1) on envoie
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
        self.dot.set("err" if err else "ok")
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
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        rows = [("Erreur", str(err))] if err else rows
        for i, (k, val) in enumerate(rows):
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
        self.resize(1120, 820)
        self.cfg, self.cards = load(), []
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(22, 18, 22, 12)
        top = QHBoxLayout()
        t = QLabel("Projecteurs")
        t.setObjectName("title")
        self.theme = QComboBox()
        self.theme.addItems(THEMES)
        self.theme.setCurrentText(self.cfg["theme"] if self.cfg["theme"] in THEMES else "Sombre")
        self.theme.currentTextChanged.connect(self.set_theme)
        rf, st = QPushButton("Actualiser"), QPushButton("Paramètres")
        st.setObjectName("accent")
        rf.clicked.connect(lambda: [c.refresh() for c in self.cards])
        st.clicked.connect(self.settings)
        for w in (t,):
            top.addWidget(w)
        top.addStretch()
        top.addWidget(QLabel("Thème"))
        top.addWidget(self.theme)
        top.addWidget(rf)
        top.addWidget(st)
        v.addLayout(top)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.holder = QWidget()
        self.grid = QGridLayout(self.holder)
        self.grid.setSpacing(18)
        self.area.setWidget(self.holder)
        v.addWidget(self.area, 1)
        self.msg = QLabel("Prêt")
        self.msg.setObjectName("sub")
        v.addWidget(self.msg)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: [c.refresh() for c in self.cards])
        self.set_theme(self.theme.currentText(), save=False)
        self.build()

    def set_theme(self, name, save=True):
        QApplication.instance().setStyleSheet(qss(THEMES[name]))
        self.cfg["theme"] = name
        if save:
            self.save()

    def save(self):
        CFG.write_text(json.dumps(self.cfg, indent=2, ensure_ascii=False), "utf-8")

    def build(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.cards = [Card(c, self.msg.setText, self) for c in self.cfg["projectors"]]
        for n, c in enumerate(self.cards):
            self.grid.addWidget(c, n // 2, n % 2)
        self.grid.setRowStretch(len(self.cards) // 2 + 1, 1)
        self.timer.stop()
        if self.cfg["poll"] > 0:
            self.timer.start(self.cfg["poll"] * 1000)
        for c in self.cards:
            c.refresh()

    def settings(self):
        d = Settings(self.cfg, self)
        if d.exec():
            self.cfg["projectors"], self.cfg["poll"] = d.projectors(), d.poll.value()
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
