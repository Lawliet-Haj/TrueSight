"""Point d'entrée figé (PyInstaller) de l'agent TrueSight.

Ce script est utilisé comme point d'entrée du build .exe. Il est exécuté comme
script TOP-LEVEL (module ``__main__``), donc il doit utiliser des imports
ABSOLUS — contrairement à ``truesight_agent/__main__.py`` qui, lui, fait des
imports relatifs (``from . import ...``) valides uniquement quand le paquet est
importé normalement (``python -m truesight_agent``).

Pointer PyInstaller directement sur ``truesight_agent/__main__.py`` casse les
imports relatifs (« attempted relative import with no known parent package ») :
on délègue donc ici au paquet via un import absolu.
"""

import sys

from truesight_agent.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
