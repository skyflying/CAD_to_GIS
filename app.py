import sys
import os

os.environ["QT_LOGGING_RULES"] = "qt.qpa.fonts.debug=false;qt.webengine.*=false"

from PySide6.QtWidgets import QApplication
from ui.main_window import MainWindow

def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
