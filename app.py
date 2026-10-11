import sys
import os

__version__ = "1.5.0"

os.environ["QT_LOGGING_RULES"] = "qt.qpa.fonts.debug=false;qt.webengine.*=false"
# allow "python path/to/app.py" from any working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importlib.util

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

# (import name, pip name)
REQUIRED = [("geopandas", "geopandas"), ("pandas", "pandas"), ("shapely", "shapely"), ("pyproj", "pyproj")]


def _missing_packages():
    def has(m):
        try:
            return importlib.util.find_spec(m) is not None
        except Exception:
            return False
    missing = [pip for mod, pip in REQUIRED if not has(mod)]
    if not (has("pyogrio") or has("fiona")):
        missing.append("pyogrio")
    # DXF reader: ezdxf (preferred) or GDAL's Python bindings (osgeo)
    if not has("ezdxf") and not has("osgeo"):
        missing.append("ezdxf")
    return missing


def main():
    app = QApplication(sys.argv)
    missing = _missing_packages()
    if missing:
        req = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
        cmd = f'"{sys.executable}" -m pip install -r "{req}"'
        msg = ("Missing Python packages: " + ", ".join(missing) +
               "\n\nInstall them into THIS Python interpreter, then start the app again:\n\n" + cmd)
        print(msg, file=sys.stderr)
        box = QMessageBox(QMessageBox.Icon.Critical, "CAD to GIS – missing packages", msg)
        box.setTextInteractionFlags(box.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse)
        box.exec()
        sys.exit(1)

    from ui.main_window import MainWindow   # imported after the check
    w = MainWindow()
    w.setWindowTitle(f"CAD to GIS Converter v{__version__}")
    w.show()
    rc = app.exec()
    # Destroy the window (and its QWebEngineView) explicitly while QApplication
    # still exists; leaving it to Python's GC at shutdown could crash on exit.
    import shiboken6
    shiboken6.delete(w)
    sys.exit(rc)

if __name__ == "__main__":
    main()
