"""Protocole série Christie HS (D16/D20-HS) sur TCP, port 3002.
Format : (CMD) pour une commande, (CMD?) pour une requête, réponse (CMD!valeur)."""
import re, socket, threading, time

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


class Projector:
    def __init__(self, ip, port=PORT):
        self.ip, self.port = ip, int(port)
        self.bind = None  # IP locale (carte réseau choisie) ou None = automatique
        self.lock = threading.Lock()  # une seule conversation TCP à la fois par VP

    def ask(self, *cmds, timeout=3.0, idle=0.35, quiet=False, want=1):
        """Envoie des commandes, renvoie la liste des messages reçus (sans parenthèses)."""
        with self.lock:
            s = socket.create_connection((self.ip, self.port), timeout=timeout,
                                         source_address=(self.bind, 0) if self.bind else None)
            with s:
                for c in cmds:
                    s.sendall(f"({c})\n".encode())
                    time.sleep(0.05)
                buf, end = "", time.time() + timeout
                s.settimeout(idle)
                while time.time() < end:
                    try:
                        d = s.recv(4096)
                        if not d:
                            break
                        buf += d.decode("latin-1")
                    except socket.timeout:
                        if quiet or len(MSG.findall(buf)) >= want:  # quiet : pas d'accusé attendu (SET)
                            break
        return [m.strip() for m in MSG.findall(buf)]

    def _get(self, cmd, key):
        return dict(parse(self.ask(cmd))).get(key)

    def power(self):   return _power(self._get("PWR?", "PWR"))
    def shutter(self): return _shutter(self._get("SHU?", "SHU"))
    def source(self):  return _source(self._get("SIN+MAIN?", "SIN+MAIN"))

    def state(self):
        # want=3 : on attend les 3 réponses (le VP peut répondre lentement) ...
        d = dict(parse(self.ask("PWR?", "SHU?", "SIN+MAIN?", want=3)))
        for cmd, key in (("PWR?", "PWR"), ("SHU?", "SHU"), ("SIN+MAIN?", "SIN+MAIN")):
            if key not in d:  # ... et on redemande individuellement celles qui manquent
                try:
                    v = self._get(cmd, key)
                except OSError:
                    v = None
                if v is not None:
                    d[key] = v
        out = {"pwr": _power(d.get("PWR")), "shu": _shutter(d.get("SHU")), "src": _source(d.get("SIN+MAIN"))}
        # signal réel sur l'entrée : seulement si le VP est allumé et sur une source identifiée
        out["sig"] = self.signal() if out["pwr"] == "on" and out["src"] is not None else None
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
            v = dict(parse(self.ask("MIF+RESL?", timeout=2.0))).get("MIF+RESL")
        except OSError:
            return None
        if v is None:
            return None
        s = v.replace('"', "")
        m = re.search(r"(\d{1,5})\s*[x×X*]\s*(\d{1,5})", s) or re.search(r"^\s*(\d{1,5})\s+(\d{1,5})\b", s)
        if m:
            w, h = int(m[1]), int(m[2])
            return (w, h) if w > 0 and h > 0 else False
        return False if re.search(r"no\s*signal|none|aucun|^\s*0+\s*$", s, re.I) else None

    def last_error(self):
        """Dernière erreur système (LSE?) : (code, texte) ; code 0 = aucune ; None = illisible."""
        try:
            v = dict(parse(self.ask("LSE?", timeout=2.0))).get("LSE")
        except OSError:
            return None
        return None if v is None else _num_name(v)

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
