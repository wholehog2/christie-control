THEMES = {  # le premier est le thème par défaut
    "Sombre":      dict(bg="#0f1115", card="#181b22", text="#e6e8ee", sub="#8b93a7", accent="#4f8cff", border="#262b36", btn="#222733", btn_h="#2c3342"),
    "Minuit":      dict(bg="#0b1220", card="#111a2e", text="#dbe6ff", sub="#7f93bd", accent="#38bdf8", border="#1c2a47", btn="#172340", btn_h="#1f2f55"),
    "Forêt":       dict(bg="#0e1512", card="#15201b", text="#e1efe6", sub="#86a393", accent="#34d399", border="#213229", btn="#1b2a23", btn_h="#25382e"),
    "Crépuscule":  dict(bg="#2a2438", card="#342d46", text="#f1ebff", sub="#b3a6d3", accent="#c084fc", border="#453c5e", btn="#3d3554", btn_h="#4b4268"),
    "Clair":       dict(bg="#f3f5f9", card="#ffffff", text="#1b2230", sub="#6b7587", accent="#2563eb", border="#dde2ec", btn="#eef1f7", btn_h="#e1e7f2"),
    "Sable":       dict(bg="#f1ece2", card="#fbf7ee", text="#352e24", sub="#85775f", accent="#0f766e", border="#e3d9c4", btn="#efe6d3", btn_h="#e6dbc2"),
}


def qss(t):
    return f"""
QWidget{{background:transparent;color:{t['text']};font-size:13px}}
QMainWindow,QDialog{{background:{t['bg']}}}
QFrame#card{{background:{t['card']};border:1px solid {t['border']};border-radius:14px}}
QLabel#title{{font-size:17px;font-weight:600}}
QLabel#sub{{color:{t['sub']}}}
QLabel#cap{{color:{t['sub']};font-size:12px;font-weight:600}}
QPushButton{{background:{t['btn']};border:1px solid {t['border']};border-radius:8px;padding:8px 12px}}
QPushButton:hover{{background:{t['btn_h']}}}
QPushButton:pressed{{background:{t['accent']};color:white}}
QPushButton#accent{{background:{t['accent']};color:white;border:none}}
QLineEdit,QSpinBox,QComboBox,QTableWidget,QPlainTextEdit{{background:{t['card']};border:1px solid {t['border']};border-radius:8px;padding:5px;selection-background-color:{t['accent']}}}
QComboBox QAbstractItemView{{background:{t['card']};color:{t['text']};selection-background-color:{t['accent']}}}
QHeaderView::section{{background:{t['btn']};color:{t['text']};border:none;padding:6px}}
QTableWidget{{gridline-color:{t['border']}}}
QScrollArea{{border:none}}
QFrame#pane{{background:{t['bg']};border:1px solid {t['border']};border-radius:12px}}
QPushButton[st="ok"]{{background:{t['accent']};color:white;border-color:{t['accent']}}}
QPushButton[st="busy"]{{border-color:#f59e0b}}
QPushButton[st="err"]{{border-color:#ef4444}}
QPushButton[sel="1"]{{background:{t['accent']};color:white}}
QPushButton#pwr{{min-width:84px;max-width:84px;min-height:84px;max-height:84px;border:4px solid {t['border']};padding:0;border-radius:42px}}
QPushButton#pwr[st="ok"]{{background:transparent;border-color:#22c55e}}
QPushButton#pwr[st="busy"]{{background:transparent;border-color:#f59e0b}}
QPushButton#pwr[st="err"]{{background:transparent;border-color:#ef4444}}
QLabel#lcd{{font-family:Menlo,Consolas,monospace;font-size:18px;color:#4ade80;background:#0b140d;border-radius:8px;padding:8px}}
QScrollBar:vertical{{width:10px;background:transparent}}
QScrollBar::handle:vertical{{background:{t['border']};border-radius:5px;min-height:30px}}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0}}
"""
