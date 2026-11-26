"""Protocole série Christie HS (D16/D20-HS) sur TCP, port 3002.
Format : (CMD) pour une commande, (CMD?) pour une requête, réponse (CMD!valeur)."""
import re, socket, threading, time
from collections import deque
from contextlib import contextmanager

PORT = 3002
INPUTS = {1: "VGA", 3: "HDMI 1", 4: "HDMI 2", 5: "DVI-D", 7: "3G-SDI", 8: "HDBaseT"}
INFO = [  # (requête, libellé)
    ("ILI?", "Source lumineuse (heures)"), ("FVI?", "Firmware"), ("ICI?", "Réseau"),
    ("LSE?", "Dernière erreur système"), ("MIF+RESL?", "Résolution entrée"),
    ("MIF+SGFT?", "Format du signal"), ("MIF+HREF?", "Fréquence horizontale"),
    ("MIF+VREF?", "Fréquence verticale"), ("SIV?", "Version des commandes"),
]
# Codes de LSE? (dernière erreur système) d'après la doc Christie HS Series
LSE_TEXT = {1: "la source lumineuse ne s'est pas allumée", 3: "la source lumineuse s'est éteinte de façon inattendue",
            4: "panne de ventilateur", 5: "surchauffe"}
MSG = re.compile(r"\(([^()]*)\)")
# Réponse complète, même si la valeur contient des parenthèses : « (ILI!000 2090 "Heures (total)") ».
# Un message se termine par « ) » suivi d'un saut de ligne, d'un autre « ( » ou de la fin des données.
MSG_FULL = re.compile(r"\(\s*([#A-Za-z]{3}.*?)\)(?=\s*(?:\(|\Z))", re.S)


def split_msgs(buf):
    """Texte reçu -> liste de messages sans les parenthèses extérieures (valeurs avec parenthèses comprises)."""
    out = [m.strip() for m in MSG_FULL.findall(buf)]
    return out if out else [m.strip() for m in MSG.findall(buf)]
# Voyants d'activité : l'appli y branche des fonctions appelées à chaque envoi / réception (depuis n'importe quel thread)
ACTIVITY = {"tx": None, "rx": None}


def _blink(kind):
    f = ACTIVITY.get(kind)
    if f:
        try:
            f()
        except Exception:
            pass
REPLY = re.compile(r"^#?([A-Za-z]{3})(?:\+([A-Za-z0-9]{4}))?!\s*(.*)$", re.S)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


NAMES = {_norm(v): k for k, v in INPUTS.items()}


def parse(msgs):
    """Messages bruts -> liste de (clé, valeur). Les erreurs deviennent ('ERR', texte)."""
    out = []
    for m in msgs:
        r = REPLY.match(m.strip())
        if r:
            out.append((r[1].upper() + ("+" + r[2].upper() if r[2] else ""), r[3].strip()))
        elif "ERR" in m:
            out.append(("ERR", m.strip()))
    return out


def _num_name(v):
    n = re.match(r"\s*(\d+)", v or "")
    q = re.search(r'"([^"]*)"', v or "")
    return (int(n[1]) if n else None), (q[1] if q else "")


def _power(v):
    if v is None:
        return None
    n, name = _num_name(v)
    name = name.lower()
    if name == "on" or (not name and n == 1):
        return "on"
    if name in ("off", "standby") or (not name and n == 0):
        return "off"
    return "busy"  # montée en puissance / refroidissement


def _shutter(v):
    n, _ = _num_name(v)
    return {0: "open", 1: "closed"}.get(n)


def _source(v):
    n, name = _num_name(v)
    if _norm(name) in NAMES:  # le nom fait foi (la doc Christie est ambiguë sur les numéros)
        return NAMES[_norm(name)]
    return n if n in INPUTS else None


# ------------------------------------------------------------------ données du projecteur
FW_LABELS = {"1": "F-MCU", "2": "S-MCU", "3": "M-MCU", "4": "L-MCU", "5": "K-MCU", "6": "LAN", "7": "Formateur",
             "8": "HDBaseT", "A": "FPGA 1", "B": "FPGA 2", "C": "FPGA 3"}
SUB_LABELS = {"MDLN": "Modèle", "SNUM": "N° de série", "NERS": "Résolution native", "RELV": "Version de la release",
              "ETH0": "Adresse IP", "SUB0": "Masque", "GATE": "Passerelle", "MAC0": "Adresse MAC", "DHCP": "DHCP",
              "HOST": "Nom d'hôte", "PDNS": "DNS", "SDNS": "DNS 2", "RESL": "Résolution", "SGFT": "Format du signal",
              "VREF": "Fréquence verticale", "HREF": "Fréquence horizontale"}


LABEL_FR = {"ip address": "Adresse IP", "subnet mask": "Masque", "gateway": "Passerelle", "mac address": "Adresse MAC",
            "primary dns": "DNS principal", "system temperature": "Température système", "secondary dns": "DNS secondaire", "dhcp": "DHCP"}


def clean_rows(pairs):
    """[(clé, valeur)] -> [(clé, libellé, valeur)] : guillemets retirés, formats courants décomposés.
    « 000 2090 Total Projector Hours » -> (« Total Projector Hours », « 2090 ») ; « Libellé : valeur » ; sous-codes connus."""
    out = []
    for key, value in pairs:
        if key == "ERR":
            continue
        v = str(value).replace('"', "").strip()
        sub = key.split("+", 1)[1] if "+" in key else ""
        m = re.match(r"^\s*\d{1,3}\s+(\S+)\s+(.+?)\s*$", v)
        m2 = re.match(r"^([A-Za-z][^:=]{2,40}?)\s*[:=]\s*(.+)$", v)
        if m:
            label, val = m[2], m[1]
        elif m2:
            label, val = m2[1].strip(), m2[2].strip()
        elif sub.startswith("FWV"):
            label, val = "Firmware " + FW_LABELS.get(sub[3:], sub[3:]), v
        elif sub in SUB_LABELS:
            label, val = SUB_LABELS[sub], v
        else:
            label, val = key, v
        label = LABEL_FR.get(label.strip().lower(), label)
        out.append((key, label, val))
    return out


def classify(label, value, key=""):
    """Catégorie d'une donnée : model, hours, versions, signal, network ou other."""
    sub = key.split("+", 1)[1].lower() if "+" in key else ""
    lab = label.lower()
    if re.search(r"\bd\d{2}[a-z]{2}\d?[- ]?hs\b", f"{label} {value}".lower()) or sub in ("mdln", "modl") \
            or re.search(r"model|modèle|modele", lab):
        return "model"
    if re.search(r"hour|heure|\bhrs?\b", lab):
        return "hours"
    if sub.startswith("fwv") or sub == "relv" or re.search(r"version|firmware|release|\bfw\b|mcu|fpga|formatter|formateur|hdbase|\blan\b", lab):
        return "versions"
    if sub in ("resl", "sgft", "vref", "href", "pixc") or re.search(r"resolution|résolution|signal|refresh|fréq|freq", lab):
        return "signal"
    if sub in ("eth0", "sub0", "gate", "mac0", "dhcp", "host", "pdns", "sdns") \
            or re.search(r"\bip\b|subnet|masque|gateway|passerelle|\bmac\b|dhcp|dns|host|network|réseau|reseau|ethernet", lab):
        return "network"
    return "other"


def _rpm(v):
    """Vitesse d'un ventilateur (tr/min) dans une réponse de FAN+FA0n? ; None si illisible."""
    if v is None:
        return None
    s = re.sub(r"(?<=\d)[ \u00a0](?=\d{3}\b)", "", str(v).replace('"', ""))  # « 4 210 » -> « 4210 »
    m = re.search(r"rpm\s*[:=]\s*(\d+(?:[.,]\d+)?)", s, re.I)  # réponse réelle : « RPM:2313, Duty:064 »
    if m:
        return int(float(m[1].replace(",", ".")))
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*rpm", s, re.I)
    if m:
        return int(float(m[1].replace(",", ".")))
    m = re.match(r"^\s*\d{1,3}\s+(\d{2,6})\b", s)  # « 001 4210 Fan 1 »
    if m:
        return int(m[1])
    s = re.sub(r"\b(fan|fa|ventilo\w*)\s*0*\d+\b", " ", s, flags=re.I)
    nums = [int(x) for x in re.findall(r"\d+", s)]
    return max(nums) if nums else None


def reply_state(msgs, group):
    """État annoncé par le VP dans sa réponse à une commande (None si la réponse n'en contient pas)."""
    d = dict(parse(msgs))
    if group == "pwr":
        return _power(d.get("PWR"))
    if group == "shu":
        return _shutter(d.get("SHU"))
    if group == "src":
        return _source(d.get("SIN+MAIN"))
    return None


def _parse_signal(v):
    """Réponse de MIF+RESL? -> (largeur, hauteur) ; False = pas de signal ; None = indéterminé."""
    if v is None:
        return None
    s = v.replace('"', "")
    m = re.search(r"(\d{1,5})\s*[x×X*]\s*(\d{1,5})", s) or re.search(r"^\s*(\d{1,5})\s+(\d{1,5})\b", s)
    if m:
        w, h = int(m[1]), int(m[2])
        return (w, h) if w > 0 and h > 0 else False
    return False if re.search(r"no\s*signal|none|aucun|^\s*0+\s*$", s, re.I) else None


class Projector:
    def __init__(self, ip, port=PORT):
        self.ip, self.port = ip, int(port)
        self.bind = None  # IP locale (carte réseau choisie) ou None = automatique
        self.lock = threading.Lock()  # une seule conversation TCP à la fois par VP
        self._tl = threading.local()
        self._urg, self._urg_lock = 0, threading.Lock()
        self.own = deque(maxlen=512)  # (ip locale, port) de nos propres connexions : l'écoute réseau les ignore

    @contextmanager
    def priority(self):
        """Actions de l'utilisateur : tant qu'elles sont en cours, les lectures de fond cèdent la place."""
        with self._urg_lock:
            self._urg += 1
        self._tl.on = True
        try:
            yield
        finally:
            self._tl.on = False
            with self._urg_lock:
                self._urg -= 1

    def ask(self, *cmds, timeout=3.0, idle=0.25, quiet=False, want=1):
        """Envoie des commandes, renvoie la liste des messages reçus (sans parenthèses)."""
        if not getattr(self._tl, "on", False):  # lecture de fond : on laisse passer les actions de l'utilisateur
            t0 = time.time()
            while self._urg and time.time() - t0 < 6.0:
                time.sleep(0.03)
        with self.lock:
            s = socket.create_connection((self.ip, self.port), timeout=timeout,
                                         source_address=(self.bind, 0) if self.bind else None)
            with s:
                self.own.append(tuple(s.getsockname()[:2]))
                for c in cmds:
                    s.sendall(f"({c})\n".encode())
                    _blink("tx")
                    time.sleep(0.05)
                buf, end = "", time.time() + timeout
                s.settimeout(idle)
                while time.time() < end:
                    try:
                        d = s.recv(4096)
                        if not d:
                            break
                        buf += d.decode("latin-1")
                        _blink("rx")
                    except socket.timeout:
                        if quiet or len(split_msgs(buf)) >= want:  # quiet : pas d'accusé attendu (SET)
                            break
        return split_msgs(buf)

    def _get(self, cmd, key):
        return dict(parse(self.ask(cmd))).get(key)

    def power(self):   return _power(self._get("PWR?", "PWR"))
    def shutter(self): return _shutter(self._get("SHU?", "SHU"))
    def source(self):  return _source(self._get("SIN+MAIN?", "SIN+MAIN"))

    def state(self):
        """Alimentation, shutter, source et signal en UNE conversation (rapide, la liaison reste libre)."""
        msgs = self.ask("PWR?", "SHU?", "SIN+MAIN?", "MIF+RESL?", want=3)
        if not msgs:  # connexion acceptée mais aucune réponse : le VP est injoignable en pratique
            raise OSError("aucune réponse du projecteur")
        d = dict(parse(msgs))
        pw = _power(d.get("PWR"))
        for cmd, key in (("PWR?", "PWR"), ("SHU?", "SHU"), ("SIN+MAIN?", "SIN+MAIN")):
            # réponse manquante : on la redemande, sauf en veille où shutter et source sont indisponibles
            if key not in d and (key == "PWR" or pw in (None, "on")):
                try:
                    v = self._get(cmd, key)
                except OSError:
                    v = None
                if v is not None:
                    d[key] = v
        out = {"pwr": _power(d.get("PWR")), "shu": _shutter(d.get("SHU")), "src": _source(d.get("SIN+MAIN"))}
        if out["pwr"] == "on" and out["src"] is not None:  # signal réellement reçu sur l'entrée
            out["sig"] = _parse_signal(d["MIF+RESL"]) if "MIF+RESL" in d else self.signal()
        else:
            out["sig"] = None
        return out

    def _read_num(self, code):
        try:
            d = dict(parse(self.ask(code + "?", timeout=2.5)))
        except OSError:
            return None
        n, _ = _num_name(d.get(code))
        return n

    def orientation(self):
        """Orientation lue sur le VP si il sait répondre : 0 / 1 (2 = auto) ou None si illisible."""
        return {"ceiling": self._read_num("CEL"), "rear": self._read_num("SOR")}

    def set_and_check(self, cmd, code, value):
        """Envoie cmd avec accusé complet (#), puis relit l'état s'il est lisible."""
        msgs = self.ask("#" + cmd, timeout=4.0)
        if any("ERR" in m for m in msgs):
            return False
        got = self._read_num(code)
        if got in (0, 1):
            return got == value
        return bool(msgs)  # état illisible : on se fie à l'accusé de réception du VP

    def signal(self):
        """Signal réellement reçu : (largeur, hauteur) ; False = pas de signal ; None = indéterminé."""
        try:
            return _parse_signal(dict(parse(self.ask("MIF+RESL?", timeout=2.0))).get("MIF+RESL"))
        except OSError:
            return None

    def last_error(self):
        """Dernière erreur système (LSE?) : (code, texte) ; code 0 = aucune ; None = illisible."""
        try:
            v = dict(parse(self.ask("LSE?", timeout=2.0))).get("LSE")
        except OSError:
            return None
        return None if v is None else _num_name(v)

    def fans(self):
        """Vitesse (tr/min) des ventilateurs 1 à 3 : [rpm | None] x 3."""
        try:
            d = dict(parse(self.ask("FAN+FA01?", "FAN+FA02?", "FAN+FA03?", timeout=1.5, want=3)))
        except OSError:
            return [None, None, None]
        out = [_rpm(d.get(f"FAN+FA0{i}")) for i in (1, 2, 3)]
        for i in (1, 2, 3):  # réponse manquante : on la redemande seule, pour que les trois valeurs arrivent ensemble
            if out[i - 1] is None:
                try:
                    out[i - 1] = _rpm(dict(parse(self.ask(f"FAN+FA0{i}?", timeout=1.5))).get(f"FAN+FA0{i}"))
                except OSError:
                    pass
        return out

    def details(self):
        """Données rangées par catégories (voir classify) + dernière erreur + ventilateurs.
        Toutes les requêtes partent dans UNE seule conversation : rapide, et la liaison reste libre."""
        d = {"model": "", "hours": [], "signal": [], "network": [], "versions": [], "other": [], "fans": [], "lse": None,
             "temp": None}
        seen = {c: set() for c in ("hours", "signal", "network", "versions", "other")}
        sst_signal, sig, fans = [], {}, {}

        def add(cat, label, val):
            if cat == "model":
                d["model"] = d["model"] or val
                return
            k = re.sub(r"\W", "", label.lower()) + ("" if label != "Réponse brute" else val)
            if k not in seen[cat]:
                seen[cat].add(k)
                d[cat].append((label, val))
        # SST? renvoie beaucoup de lignes : sa conversation à part attend la fin du flux (1 s de silence), au lieu
        # de s'arrêter dès qu'on a « assez » de messages et de perdre la fin de la liste ou les réponses suivantes.
        msgs = []
        for c, t in (("ILI?", 5.0), ("FVI?", 5.0), ("ICI?", 6.0), ("SST?", 12.0)):  # une conversation par liste
            got = self.ask(c, timeout=t, idle=(1.0 if c == "SST?" else 0.7), want=2)
            if not got and c == "ICI?":  # réseau : une relecture si rien n'est arrivé
                got = self.ask(c, timeout=t, idle=(1.0 if c == "SST?" else 0.7), want=2)
            msgs += got
        msgs += self.ask("MIF+RESL?", "MIF+SGFT?", "MIF+VREF?", "LSE?", "FAN+FA01?", "FAN+FA02?", "FAN+FA03?",
                         timeout=4.0, idle=0.5, want=7)
        if not msgs:
            raise OSError("aucune réponse du projecteur")
        for m in msgs:  # réponse d'un format inattendu : on l'affiche quand même au lieu de la perdre
            if not REPLY.match(m) and "ERR" not in m:
                add("other", "Réponse brute", m)
        for key, label, val in clean_rows(parse(msgs)):
            head = key.split("+")[0]
            if head == "LSE":
                d["lse"] = _num_name(val)
            elif head == "FAN":
                fans[key] = val
            elif key.startswith("MIF+"):
                sig[key[4:]] = val
            else:
                default = {"ILI": "hours", "FVI": "versions", "ICI": "network"}.get(head)
                cat = classify(label, val, key)
                cat = cat if (default is None or cat == "model") else default
                if cat == "signal" and default is None:
                    sst_signal.append((label, val))
                else:
                    add(cat, label, val)
        for code, label in (("RESL", "Résolution"), ("SGFT", "Format du signal"), ("VREF", "Fréquence verticale")):
            if sig.get(code) is not None:
                d["signal"].append((label, sig[code]))
        if not d["signal"]:
            d["signal"] = sst_signal
        if not d["model"]:  # PIF n'est disponible qu'en mode service : simple essai
            try:
                v = dict(parse(self.ask("PIF+MDLN?", timeout=1.5))).get("PIF+MDLN")
                d["model"] = (v or "").replace('"', "").strip()
            except OSError:
                pass
        # PIP : retiré de l'affichage ; température système : sortie des listes (affichée à côté des heures)
        d["temp"] = None
        for cat in ("hours", "signal", "network", "versions", "other"):
            keep = []
            for label, val in d[cat]:
                if re.search(r"(?<![a-z])pip(?![a-z])|picture.?in.?picture", label, re.I):
                    continue
                if d["temp"] is None and re.search(r"temp", label, re.I) and cat in ("versions", "other"):
                    d["temp"] = (label, val)
                    continue
                keep.append((label, val))
            d[cat] = keep
        d["fans"] = [_rpm(fans.get(f"FAN+FA0{i}")) for i in (1, 2, 3)]
        if None in d["fans"]:
            d["fans"] = self.fans()
        return d

    def info(self):
        rows = []
        for cmd, label in INFO:
            pairs = parse(self.ask(cmd))
            for k, v in pairs:
                if k == "ERR":
                    rows.append((label, "non disponible"))
                else:
                    rows.append((label if len(pairs) == 1 else f"{label} · {k}", v.replace('"', "")))
        return rows


def wait_for(getter, target, timeout, every=1.5):
    """Interroge getter() jusqu'à obtenir target (ou délai dépassé) -> bool."""
    end = time.time() + timeout
    while True:
        try:
            if getter() == target:
                return True
        except OSError:
            pass
        if time.time() > end:
            return False
        time.sleep(every)
