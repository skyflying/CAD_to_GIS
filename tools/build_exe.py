# -*- coding: utf-8 -*-
"""Build a Windows/macOS/Linux executable with PyInstaller:
    pip install pyinstaller
    python tools/build_exe.py
Output: dist/CAD_to_GIS/  (one-folder build; QtWebEngine does not work well as one-file)."""
import os
import sys

import PyInstaller.__main__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sep = ";" if os.name == "nt" else ":"
args = [
    os.path.join(ROOT, "app.py"),
    "--name", "CAD_to_GIS",
    "--noconfirm", "--windowed",
    "--add-data", f"{os.path.join(ROOT, 'ui', 'web')}{sep}ui/web",   # Leaflet for offline map
    "--paths", ROOT,
]
for pkg in ("pyogrio", "pyproj", "shapely", "ezdxf", "geopandas"):
    args += ["--collect-all", pkg]
PyInstaller.__main__.run(args + sys.argv[1:])
