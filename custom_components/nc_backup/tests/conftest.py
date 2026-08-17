"""Macht `client.py`/`const.py` isoliert importierbar, OHNE das echte
`nc_backup/__init__.py` auszufuehren (das importiert `homeassistant.*`,
was in dieser Dev-Umgebung nicht installiert ist und auch nicht sein muss --
client.py selbst hat bewusst keine HA-Abhaengigkeit, siehe Plan Abschnitt 9d:
"Unit-Tests ohne laufendes HA").

Trick: ein leeres Namespace-Package `nc_backup` in sys.modules registrieren,
dessen __path__ auf den echten Ordner zeigt. `from nc_backup import client`
findet darueber client.py ganz normal und loest dessen relatives
`from .const import ...` korrekt gegen nc_backup.const auf -- ohne je
nc_backup/__init__.py auszufuehren.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parents[1]

if "nc_backup" not in sys.modules:
    _pkg = types.ModuleType("nc_backup")
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["nc_backup"] = _pkg
