"""Écoute passive du réseau : repère les ordres envoyés au projecteur par d'autres logiciels (même sur un autre
ordinateur) et les réponses du projecteur, pour que l'appli suive l'état sans attendre l'actualisation.

Le projecteur ne répond qu'à celui qui l'interroge : la seule façon de voir les échanges des autres est de lire
les paquets TCP qui passent par la carte réseau de cet ordinateur. Cela demande :
  - le module Python « scapy » (pip install scapy) ;
  - Windows : Npcap installé (https://npcap.com) ; Linux / macOS : droits administrateur (root) ;
  - que ces paquets arrivent jusqu'à cet ordinateur : sur un switch, il faut une recopie de port (port mirroring)
    vers cet ordinateur, ou que les échanges passent par lui (hub, pont...). Sans cela on ne voit que ses propres échanges.
"""
import os, re, sys, threading
from collections import deque

MSG = re.compile(r"\(([^()]*)\)")
NPCAP_URL = "https://npcap.com/#download"


def npcap_installed():
    """Windows : Npcap (ou l'ancien WinPcap) est-il installé ? Ailleurs : toujours True (rien à installer)."""
    if sys.platform != "win32":
        return True
    root = os.environ.get("SystemRoot", r"C:\Windows")
    for f in (("System32", "Npcap", "wpcap.dll"), ("System32", "wpcap.dll")):
        if os.path.exists(os.path.join(root, *f)):
            return True
    try:
        import winreg
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Npcap", 0, winreg.KEY_READ | view))
                return True
            except OSError:
                pass
    except ImportError:
        pass
    return False


class NetListener:
    """targets : {(ip, port)} des projecteurs ; is_own((ip, port)) : l'échange vient-il de l'appli elle-même ?
    on_msgs((ip, port), from_vp, [messages]) et on_status(ok, texte) sont appelés depuis le thread d'écoute."""

    def __init__(self, targets, is_own, on_msgs, on_status):
        self.targets, self.is_own = set(targets), is_own
        self.on_msgs, self.on_status = on_msgs, on_status
        self._sniffer, self._stopped = None, False
        self._buf, self._seen = {}, {}

    # ------------------------------------------------------------------ cycle de vie
    def start(self):
        threading.Thread(target=self._start, daemon=True).start()

    def _start(self):
        try:
            from scapy.all import AsyncSniffer, conf
        except Exception as e:
            self.on_status(False, f"module « scapy » absent ({e}) : pip install scapy")
            return
        try:
            conf.verb = 0
            ifaces = []
            for ip, _ in self.targets:
                try:
                    i = conf.route.route(ip)[0]
                except Exception:
                    i = None
                if i and i not in ifaces:
                    ifaces.append(i)
            kw = {"iface": ifaces} if ifaces else {}
            ports = sorted({p for _, p in self.targets})
            bpf = "tcp and (" + " or ".join(f"port {p}" for p in ports) + ")"
            last = None
            for flt in (bpf, None):  # sans tcpdump / libpcap le filtre BPF échoue : on filtre alors en Python
                try:
                    s = AsyncSniffer(prn=self._packet, store=False, filter=flt, **kw)
                    s.start()
                    self._wait_started(s)
                    self._sniffer = s
                    break
                except Exception as e:
                    last = e
            else:
                raise last
            if self._stopped:
                self.stop()
                return
            self.on_status(True, "écoute des paquets sur " + (", ".join(map(str, ifaces)) or "l'interface par défaut"))
        except PermissionError:
            self.on_status(False, "droits insuffisants : lance l'appli en administrateur (root sous Linux/macOS)")
        except Exception as e:
            self.on_status(False, f"impossible d'écouter le réseau : {e} (Npcap installé ? droits administrateur ?)")

    @staticmethod
    def _wait_started(s):
        """Laisse à scapy le temps d'ouvrir la carte : une erreur (filtre BPF impossible, droits...) arrive un peu
        après le démarrage du thread."""
        import time
        for _ in range(8):
            time.sleep(0.1)
            if getattr(s, "exception", None):
                break
        if getattr(s, "exception", None):
            try:
                s.stop()
            except Exception:
                pass
            raise s.exception
        if not getattr(s, "running", False):
            raise RuntimeError("l'écoute n'a pas démarré")

    def stop(self):
        self._stopped = True
        s, self._sniffer = self._sniffer, None
        if s is not None:
            try:
                s.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------ paquets
    def _packet(self, pkt):
        try:
            self._handle(pkt)
        except Exception:
            pass  # un paquet illisible ne doit jamais arrêter l'écoute

    def _handle(self, pkt):
        from scapy.layers.inet import IP, TCP
        if self._stopped or IP not in pkt or TCP not in pkt:
            return
        ip, tcp = pkt[IP], pkt[TCP]
        key = (ip.src, tcp.sport)
        flow_end = bool(tcp.flags & 0x05)  # FIN ou RST : l'échange est terminé
        if key in self.targets:
            vp, client, from_vp = key, (ip.dst, tcp.dport), True
        elif (ip.dst, tcp.dport) in self.targets:
            vp, client, from_vp = (ip.dst, tcp.dport), (ip.src, tcp.sport), False
        else:
            return
        if self.is_own(client):
            return
        flow = (vp, client, from_vp)
        if flow_end:
            self._buf.pop(flow, None)
            self._seen.pop(flow, None)
        payload = bytes(tcp.payload)
        if not payload:
            return
        seen = self._seen.setdefault(flow, deque(maxlen=64))
        if tcp.seq in seen:  # même segment vu deux fois (retransmission, capture en double)
            return
        seen.append(tcp.seq)
        buf = self._buf.get(flow, "") + payload.decode("latin-1")
        msgs = [m.strip() for m in MSG.findall(buf)]
        rest = buf[buf.rfind(")") + 1:] if ")" in buf else buf
        self._buf[flow] = rest[-300:]
        if len(self._buf) > 200:  # on n'accumule pas des échanges anciens sans fin
            self._buf.pop(next(iter(self._buf)), None)
            self._seen.pop(next(iter(self._seen)), None)
        if msgs:
            self.on_msgs(vp, from_vp, msgs)
