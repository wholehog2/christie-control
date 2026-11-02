#!/usr/bin/env python3
"""Simulateur de projecteurs Christie D20HD-HS (protocole série sur TCP, port 3002 par défaut).

  python3 christie_simulator.py                      -> interface graphique (2 projecteurs : ports 3002 et 3003)
  python3 christie_simulator.py --headless --on      -> sans interface, journal dans la console
  python3 christie_simulator.py --write-config f.json -> écrit une config prête pour christie-control, puis quitte

Pour lancer l'appli sur le simulateur SANS toucher à tes vrais réglages :
  CHRISTIE_CONFIG=~/christie_sim_config.json python3 app.py          (Windows : set CHRISTIE_CONFIG=... puis python app.py)
"""
import argparse, json, re, socket, sys, threading, time
from collections import deque
from pathlib import Path

INPUTS = {1: "VGA", 3: "HDMI 1", 4: "HDMI 2", 5: "DVI-D", 7: "3G-SDI", 8: "HDBaseT"}
PWR = {"off": (0, "Off"), "warming": (2, "Warming up"), "on": (1, "On"), "cooling": (3, "Cooling down")}
RESOLUTIONS = {"1920×1080": (1920, 1080), "3840×2160": (3840, 2160), "1280×720": (1280, 720)}
LSE_CODES = {0: "Aucune", 1: "1 · source lumineuse non allumée", 3: "3 · source éteinte de façon inattendue",
             4: "4 · panne de ventilateur", 5: "5 · surchauffe"}
KNOWN_WRITE = {"ITP", "FRZ", "BRT", "CON", "LPM", "LPP", "LMV", "LMA", "LMS", "LMC", "LCB", "WRE", "WAP", "PIP", "SZP",
               "NET", "NTW", "ADR", "BAC", "UID", "PCG", "PIV", "FCT", "SPP"}  # acceptées sans effet simulé
MSG = re.compile(r"\(([^()]*)\)")
CMD = re.compile(r"^\s*([A-Za-z]{3}(?:\+[A-Za-z0-9]{4})?)\s*(\?)?\s*(.*?)\s*$")


class SimVP:
    """Un projecteur virtuel : état, réponses au protocole, pannes injectables. Indépendant de Qt."""

    def __init__(self, name="VP", port=3002, host="127.0.0.1", speed=1.0):
        self.name, self.port, self.host, self.speed = name, port, host, speed
        self.lock = threading.RLock()
        self.log, self.seq = deque(maxlen=500), 0
        self.conns, self.srv, self._stop = [], None, threading.Event()
        self._gen, self.deadline = 0, None
        # --- comportements et pannes ---
        self.online, self.silent = True, False       # hors ligne (connexion refusée) / accepte mais ne répond pas
        self.latency_ms, self.max_conn = 0, 0        # délai avant chaque réponse / connexions simultanées (0 = illimité)
        self.orient_readable = True                  # CEL? / SOR? répondent-ils ?
        self.nosignal = "0x0"                        # réponse de MIF+RESL? sans signal : « 0x0 », « No Signal » ou « erreur »
        self.warm_s, self.cool_s = 25, 40
        self.lse = 0
        self.reset(first=True)

    # ---------------------------------------------------------------- état
    def reset(self, first=False):
        with self.lock:
            self._gen += 1
            self.power, self.deadline = "off", None
            self.shutter, self.source = 1, 3          # shutter fermé, HDMI 1
            self.ceiling, self.rear = 2, 0            # CEL : 2 = automatique ; SOR : 0 = normal
            self.signals = {k: k in (3, 4, 7) for k in INPUTS}
            self.res = (1920, 1080)
            self.hours, self.ld_hours = 2090, 1875
            self.extra = {}
            if not first:
                self.note("réglages d'usine (DEF 111) : état réinitialisé")

    def note(self, text):
        with self.lock:
            self.log.append((time.time(), text))
            self.seq += 1

    def log_since(self, i):
        with self.lock:
            n = len(self.log)
            first = self.seq - n
            return self.seq, [(t, x) for k, (t, x) in enumerate(self.log) if first + k >= i]

    def signal_now(self):
        return self.res if self.power == "on" and self.signals.get(self.source) else None

    def snapshot(self):
        with self.lock:
            left = max(0.0, self.deadline - time.time()) if self.deadline else 0.0
            return {"power": self.power, "left": left, "shutter": self.shutter, "source": self.source,
                    "signal": self.signal_now(), "ceiling": self.ceiling, "rear": self.rear, "lse": self.lse,
                    "conns": len(self.conns), "online": self.online}

    # ------------------------------------------------- alimentation et télécommande
    def _schedule(self, secs, nxt):
        self._gen += 1
        gen, delay = self._gen, secs / max(self.speed, 0.01)
        self.deadline = time.time() + delay

        def fire():
            with self.lock:
                if gen == self._gen:
                    self.power, self.deadline = nxt, None
                    self.note(f"état : {PWR[nxt][1]}")
        t = threading.Timer(delay, fire)
        t.daemon = True
        t.start()

    def _set_power(self, on):
        """False = refusé (refroidissement en cours)."""
        with self.lock:
            if on:
                if self.power in ("on", "warming"):
                    return True
                if self.power == "cooling":
                    return False
                self.power = "warming"
                self._schedule(self.warm_s, "on")
            else:
                if self.power in ("off", "cooling"):
                    return True
                self.power = "cooling"
                self._schedule(self.cool_s, "off")
            self.note(f"état : {PWR[self.power][1]}")
            return True

    def remote_power(self, on):
        self.note("télécommande : " + ("marche" if on else "arrêt"))
        return self._set_power(on)

    def remote_shutter(self, closed):
        with self.lock:
            if self.power == "on":
                self.shutter = 1 if closed else 0
                self.note("télécommande : shutter " + ("fermé" if closed else "ouvert"))

    def remote_source(self, n):
        with self.lock:
            if self.power == "on" and n in INPUTS:
                self.source = n
                self.note(f"télécommande : source {INPUTS[n]}")

    # ------------------------------------------------------------ protocole
    @staticmethod
    def err(n, text):
        return f'(65535 00000 ERR{n:05d} "{text}")'

    def handle(self, raw):
        """Traite une commande (sans parenthèses) -> liste de réponses (avec parenthèses)."""
        ack = raw.lstrip().startswith("#")
        m = CMD.match(raw.lstrip("# \t"))
        if not m:
            return [self.err(1, "Syntax error")]
        code, q, arg = m.group(1).upper(), bool(m.group(2)), m.group(3)
        with self.lock:
            return self.query(code) if q else self.write(code, arg, ack, raw.strip())

    def _nosignal_reply(self, tag):
        if self.nosignal == "erreur":
            return [self.err(6, "No signal")]
        return [f'({tag}!"{"No Signal" if self.nosignal == "No Signal" else "0x0"}")']

    def query(self, code):
        if code == "PWR":
            n, name = PWR[self.power]
            return [f'(PWR!{n:03d} "{name}")']
        if code in ("SHU", "SIN+MAIN", "MIF+RESL", "MIF+SGFT", "MIF+HREF", "MIF+VREF") and self.power != "on":
            return [self.err(2, "Not available in standby")]
        if code == "SHU":
            return [f"(SHU!{self.shutter:03d})"]
        if code == "SIN+MAIN":
            return [f'(SIN+MAIN!{self.source:03d} "{INPUTS[self.source]}")']
        if code.startswith("MIF+"):
            sig = self.signal_now()
            if not sig:
                return self._nosignal_reply(code)
            w, h = sig
            val = {"RESL": f"{w}x{h}", "SGFT": f"{h}p60", "HREF": f"{h * 0.0625:.1f} kHz", "VREF": "60.0 Hz"}.get(code[4:])
            return [f'({code}!"{val}")'] if val else [self.err(1, "Unknown command")]
        if code == "LSE":
            return [f"(LSE!{self.lse:03d})"]
        if code in ("CEL", "SOR"):
            if not self.orient_readable:
                return [self.err(5, "Query not supported")]
            return [f"({code}!{(self.ceiling if code == 'CEL' else self.rear):03d})"]
        if code == "ILI":
            return [f'(ILI!"000 {self.hours} Total Projector Hours")', f'(ILI!"001 {self.ld_hours} Total LD Hours")']
        if code == "FVI":
            return ['(FVI!"2.3.1")']
        if code == "ICI":
            return [f'(ICI!"{self.host}:{self.port}")']
        return [self.err(1, "Unknown command")]

    def write(self, code, arg, ack, raw):
        ok = lambda: [f"({raw})"] if ack else []   # « # » = accusé complet : écho une fois la commande traitée
        bad = lambda: [self.err(3, "Bad parameter")]
        a = arg.strip()
        if code == "PWR":
            if a not in ("0", "1"):
                return bad()
            return ok() if self._set_power(a == "1") else [self.err(4, "Busy: cooling down")]
        if code in ("SHU", "SIN+MAIN") and self.power != "on":
            return [self.err(2, "Not available in standby")]
        if code == "SHU":
            if a not in ("0", "1"):
                return bad()
            self.shutter = int(a)
            return ok()
        if code == "SIN+MAIN":
            if not (a.isdigit() and int(a) in INPUTS):
                return bad()
            self.source = int(a)
            return ok()
        if code in ("CEL", "SOR"):
            if a not in (("0", "1", "2") if code == "CEL" else ("0", "1")):
                return bad()
            setattr(self, "ceiling" if code == "CEL" else "rear", int(a))
            return ok()
        if code == "DEF":
            if a != "111":
                return bad()
            self.reset()
            return ok()
        if code in KNOWN_WRITE:
            self.extra[code] = a
            return ok()
        return [self.err(1, "Unknown command")]

    # --------------------------------------------------------------- réseau
    def start(self):
        self._stop.clear()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((self.host, self.port))
        self.srv.listen()
        self.srv.settimeout(0.3)
        threading.Thread(target=self._accept, args=(self.srv,), daemon=True).start()
        self.note(f"à l'écoute sur {self.host}:{self.port}")

    def stop(self):
        self._stop.set()
        for s in [self.srv, *list(self.conns)]:
            try:
                s.close()
            except OSError:
                pass
        self.conns.clear()
        self.srv = None

    def set_online(self, on):
        """Hors ligne = on ferme le port (connexion refusée), comme un VP débranché du réseau."""
        self.online = on
        if on and self.srv is None:
            self.start()
        elif not on and self.srv is not None:
            self.stop()
            self.note("hors ligne (connexions refusées)")

    def _accept(self, srv):
        while not self._stop.is_set():
            try:
                c, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self.max_conn and len(self.conns) >= self.max_conn:
                self.note("connexion refusée : nombre maximum de connexions atteint")
                c.close()
                continue
            self.conns.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        buf = ""
        try:
            while not self._stop.is_set():
                data = c.recv(4096)
                if not data:
                    break
                buf += data.decode("latin-1")
                while True:
                    m = MSG.search(buf)
                    if not m:
                        buf = buf[-200:]
                        break
                    buf = buf[m.end():]
                    raw = m.group(1)
                    self.note(f"← ({raw})")
                    if self.silent:
                        self.note("   (silencieux : ignoré)")
                        continue
                    for r in self.handle(raw):
                        if self.latency_ms:
                            time.sleep(self.latency_ms / 1000)
                        c.sendall(r.encode("latin-1", "replace"))
                        self.note(f"→ {r}")
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass
            if c in self.conns:
                self.conns.remove(c)


def export_config(path, vps, ip="127.0.0.1"):
    cfg = {"theme": "Sombre", "look": "classic", "poll": 3, "stagger": 1, "confirm_off": True,
           "projectors": [{"name": v.name, "ip": ip, "port": v.port, "inputs": list(INPUTS)} for v in vps]}
    Path(path).expanduser().write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
    return str(Path(path).expanduser())


# =============================================================== interface
def run_gui(args):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                                   QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
                                   QVBoxLayout, QWidget)

    STYLE = """
    QWidget{background:#0f1115;color:#e6e8ee;font-size:13px}
    QFrame#card{background:#181b22;border:1px solid #262b36;border-radius:12px}
    QLabel#sub{color:#8b93a7} QLabel#cap{color:#8b93a7;font-weight:600;font-size:12px}
    QPushButton{background:#222733;border:1px solid #262b36;border-radius:8px;padding:6px 12px}
    QPushButton:hover{background:#2c3342}
    QLineEdit,QSpinBox,QComboBox,QPlainTextEdit{background:#12151b;border:1px solid #262b36;border-radius:6px;padding:4px}
    QComboBox QAbstractItemView{background:#181b22}
    """

    class Panel(QFrame):
        def __init__(self, vp, win):
            super().__init__()
            self.vp, self.win, self.seen = vp, win, 0
            self.setObjectName("card")
            v = QVBoxLayout(self)
            h = QHBoxLayout()
            self.led = QLabel("●")
            self.name = QLineEdit(vp.name)
            self.name.textChanged.connect(lambda t: setattr(vp, "name", t))
            self.addr = QLabel()
            self.addr.setObjectName("sub")
            self.conn = QLabel()
            self.conn.setObjectName("sub")
            rm = QPushButton("Supprimer")
            rm.clicked.connect(lambda: win.remove(self))
            for w in (self.led, self.name, self.addr, self.conn):
                h.addWidget(w)
            h.addStretch()
            h.addWidget(rm)
            v.addLayout(h)
            self.state = QLabel()
            self.state.setTextFormat(Qt.RichText)
            self.state.setWordWrap(True)
            v.addWidget(self.state)

            v.addWidget(self.cap("Télécommande (agit sur le projecteur simulé, comme quelqu'un devant)"))
            r = QHBoxLayout()
            self.b_pwr, self.b_shu = QPushButton("Marche / arrêt"), QPushButton("Shutter ouvert / fermé")
            self.b_pwr.clicked.connect(lambda: vp.remote_power(vp.power in ("off", "cooling")))
            self.b_shu.clicked.connect(lambda: vp.remote_shutter(vp.shutter == 0))
            self.src = QComboBox()
            for k, n in INPUTS.items():
                self.src.addItem(n, k)
            self.src.activated.connect(lambda _: vp.remote_source(self.src.currentData()))
            for w in (self.b_pwr, self.b_shu, QLabel("Source :"), self.src):
                r.addWidget(w)
            r.addStretch()
            v.addLayout(r)

            v.addWidget(self.cap("Signal présent sur chaque entrée"))
            r = QHBoxLayout()
            for k, n in INPUTS.items():
                cb = QCheckBox(n)
                cb.setChecked(vp.signals[k])
                cb.toggled.connect(lambda on, k=k: vp.signals.__setitem__(k, on))
                r.addWidget(cb)
            self.res = QComboBox()
            self.res.addItems(RESOLUTIONS)
            self.res.activated.connect(lambda _: setattr(vp, "res", RESOLUTIONS[self.res.currentText()]))
            self.nos = QComboBox()
            self.nos.addItems(["0x0", "No Signal", "erreur"])
            self.nos.activated.connect(lambda _: setattr(vp, "nosignal", self.nos.currentText()))
            r.addStretch()
            v.addLayout(r)
            r = QHBoxLayout()
            for w in (QLabel("Résolution :"), self.res, QLabel("Sans signal, MIF+RESL? répond :"), self.nos):
                r.addWidget(w)
            r.addStretch()
            v.addLayout(r)

            v.addWidget(self.cap("Pannes et comportements à tester"))
            g = QGridLayout()
            self.off = QCheckBox("Hors ligne (connexion refusée)")
            self.off.toggled.connect(lambda on: vp.set_online(not on))
            self.sil = QCheckBox("Silencieux (accepte la connexion, ne répond pas)")
            self.sil.toggled.connect(lambda on: setattr(vp, "silent", on))
            self.rd = QCheckBox("CEL? / SOR? (plafond, rétro) lisibles")
            self.rd.setChecked(True)
            self.rd.toggled.connect(lambda on: setattr(vp, "orient_readable", on))
            self.lse = QComboBox()
            for k, n in LSE_CODES.items():
                self.lse.addItem("Dernière erreur : " + n, k)
            self.lse.activated.connect(lambda _: setattr(vp, "lse", self.lse.currentData()))
            g.addWidget(self.off, 0, 0)
            g.addWidget(self.sil, 0, 1)
            g.addWidget(self.rd, 1, 0)
            g.addWidget(self.lse, 1, 1)
            for row, (text, attr, hi) in enumerate((("Latence avant chaque réponse (ms)", "latency_ms", 5000),
                                                     ("Connexions simultanées max (0 = illimité)", "max_conn", 10),
                                                     ("Durée d'allumage (s)", "warm_s", 600),
                                                     ("Durée de refroidissement (s)", "cool_s", 900)), start=2):
                sp = QSpinBox()
                sp.setRange(0, hi)
                sp.setValue(getattr(vp, attr))
                sp.setMaximumWidth(110)
                sp.valueChanged.connect(lambda val, a=attr: setattr(vp, a, val))
                g.addWidget(QLabel(text), row, 0)
                g.addWidget(sp, row, 1)
            g.setColumnStretch(0, 1)
            v.addLayout(g)

            v.addWidget(self.cap("Journal des échanges"))
            self.logv = QPlainTextEdit()
            self.logv.setReadOnly(True)
            self.logv.setMaximumBlockCount(400)
            self.logv.setMinimumHeight(130)
            v.addWidget(self.logv)
            clr = QPushButton("Effacer le journal")
            clr.clicked.connect(self.logv.clear)
            v.addWidget(clr, 0, Qt.AlignLeft)

        @staticmethod
        def cap(t):
            l = QLabel(t)
            l.setObjectName("cap")
            return l

        def refresh(self):
            vp, s = self.vp, self.vp.snapshot()
            self.addr.setText(f"{vp.host}:{vp.port}")
            self.conn.setText(f"{s['conns']} connexion(s)")
            self.led.setStyleSheet(f"color:{'#22c55e' if s['online'] else '#ef4444'}")
            col, txt = {"off": ("#8b93a7", "ÉTEINT (veille)"), "warming": ("#f59e0b", "ALLUMAGE"),
                        "on": ("#22c55e", "ALLUMÉ"), "cooling": ("#f59e0b", "REFROIDISSEMENT")}[s["power"]]
            if s["left"]:
                txt += f" … {int(s['left']) // 60}:{int(s['left']) % 60:02d}"
            sig = f"{s['signal'][0]}×{s['signal'][1]}" if s["signal"] else "aucun"
            orient = f"plafond : {['non', 'oui', 'auto'][s['ceiling']]} · rétro : {['non', 'oui'][s['rear']]}"
            self.state.setText(
                f'<span style="color:{col};font-size:20px;font-weight:700">● {txt}</span><br>'
                f'<span style="color:#8b93a7">Shutter : <b style="color:#e6e8ee">{"fermé" if s["shutter"] else "ouvert"}</b> · '
                f'Source : <b style="color:#e6e8ee">{INPUTS[s["source"]]}</b> · Signal : <b style="color:#e6e8ee">{sig}</b> · '
                f'{orient} · Erreur : <b style="color:#e6e8ee">{LSE_CODES.get(s["lse"], s["lse"])}</b></span>')
            self.seen, lines = vp.log_since(self.seen)
            for t, x in lines:
                self.logv.appendPlainText(f"{time.strftime('%H:%M:%S', time.localtime(t))}  {x}")

    class Win(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Simulateur de projecteurs Christie")
            self.resize(1300, 920)
            self.vps, self.panels = [], []
            root = QWidget()
            self.setCentralWidget(root)
            v = QVBoxLayout(root)
            top = QHBoxLayout()
            self.host = QComboBox()
            self.host.addItem("Cet ordinateur seulement (127.0.0.1)", "127.0.0.1")
            self.host.addItem("Tout le réseau (0.0.0.0)", "0.0.0.0")
            self.host.activated.connect(lambda _: self.rebind())
            self.speed = QComboBox()
            for n, f in (("Temps réel", 1), ("Accéléré ×5", 5), ("Accéléré ×20", 20), ("Accéléré ×60", 60)):
                self.speed.addItem(n, f)
            self.speed.activated.connect(lambda _: [setattr(x, "speed", self.speed.currentData()) for x in self.vps])
            add, exp = QPushButton("Ajouter un projecteur"), QPushButton("Créer la config pour l'appli")
            add.clicked.connect(lambda: self.add_vp())
            exp.clicked.connect(self.export)
            for w in (QLabel("Écouter sur"), self.host, QLabel("Durées"), self.speed, add, exp):
                top.addWidget(w)
            top.addStretch()
            v.addLayout(top)
            self.area = QScrollArea()
            self.area.setWidgetResizable(True)
            self.holder = QWidget()
            self.grid = QGridLayout(self.holder)
            self.grid.setAlignment(Qt.AlignTop)
            self.area.setWidget(self.holder)
            v.addWidget(self.area, 1)
            self.timer = QTimer(self)
            self.timer.timeout.connect(lambda: [p.refresh() for p in self.panels])
            self.timer.start(300)

        def add_vp(self, name=None, port=None, on=False):
            port = port or (max([x.port for x in self.vps], default=3001) + 1)
            vp = SimVP(name or f"VP {len(self.vps) + 1}", port, self.host.currentData(), self.speed.currentData())
            try:
                vp.start()
            except OSError as e:
                QMessageBox.warning(self, "Port indisponible", f"Impossible d'écouter sur le port {port} : {e}")
                return
            if on:
                vp.remote_power(True)
            p = Panel(vp, self)
            self.vps.append(vp)
            self.panels.append(p)
            n = len(self.panels) - 1
            self.grid.addWidget(p, n // 2, n % 2)

        def remove(self, panel):
            i = self.panels.index(panel)
            self.panels.pop(i)
            vp = self.vps.pop(i)
            vp.stop()
            panel.deleteLater()
            for n, p in enumerate(self.panels):
                self.grid.addWidget(p, n // 2, n % 2)

        def rebind(self):
            for vp in self.vps:
                was = vp.online
                vp.stop()
                vp.host = self.host.currentData()
                if was:
                    vp.start()

        def export(self):
            ip = "127.0.0.1" if self.host.currentData() == "127.0.0.1" else self._lan_ip()
            path = export_config("~/christie_sim_config.json", self.vps, ip)
            QMessageBox.information(
                self, "Config créée",
                f"Fichier créé : {path}\n\nLance l'appli sur le simulateur (sans toucher à tes vrais réglages) :\n\n"
                f"  Linux / Mac :  CHRISTIE_CONFIG={path} python3 app.py\n"
                f"  Windows :        set CHRISTIE_CONFIG={path}  puis  python app.py\n\n"
                + ("" if ip == "127.0.0.1" else f"Adresse utilisée : {ip} (remplace-la si ton ordinateur en a une autre)."))

        @staticmethod
        def _lan_ip():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("10.255.255.255", 1))
                return s.getsockname()[0]
            except OSError:
                return "127.0.0.1"
            finally:
                s.close()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    w = Win()
    for i in range(2):
        w.add_vp(f"Simulé {i + 1}", 3002 + i, on=args.on)
    w.show()
    sys.exit(app.exec())


def run_headless(args):
    vps = []
    for i, port in enumerate(args.ports):
        vp = SimVP(f"Simulé {i + 1}", port, args.host, args.speed)
        vp.start()
        if args.on:
            vp.remote_power(True)
        vps.append(vp)
    print(f"Simulateur actif sur {args.host}, ports {args.ports} (Ctrl+C pour quitter)")
    seen = [0] * len(vps)
    try:
        while True:
            for i, vp in enumerate(vps):
                seen[i], lines = vp.log_since(seen[i])
                for t, x in lines:
                    print(f"{time.strftime('%H:%M:%S', time.localtime(t))} [{vp.port}] {x}")
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="Simulateur de projecteurs Christie D20HD-HS")
    ap.add_argument("--headless", action="store_true", help="sans interface graphique")
    ap.add_argument("--ports", type=int, nargs="+", default=[3002, 3003])
    ap.add_argument("--host", default="127.0.0.1", help="adresse d'écoute (0.0.0.0 = tout le réseau)")
    ap.add_argument("--speed", type=float, default=1.0, help="accélère allumage / refroidissement (ex. 20)")
    ap.add_argument("--on", action="store_true", help="démarre avec les projecteurs allumés")
    ap.add_argument("--write-config", metavar="FICHIER", help="écrit une config pour l'appli et quitte")
    args = ap.parse_args()
    if args.write_config:
        vps = [SimVP(f"Simulé {i + 1}", p) for i, p in enumerate(args.ports)]
        print("Config écrite :", export_config(args.write_config, vps))
    elif args.headless:
        run_headless(args)
    else:
        run_gui(args)


if __name__ == "__main__":
    main()
