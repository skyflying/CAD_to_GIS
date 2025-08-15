# dxf2gis_gui/ui/main_window.py
from __future__ import annotations
import os
import pathlib
import time
import traceback
from typing import List, Optional, Set

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFileDialog, QMessageBox,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget, QListWidgetItem,
    QLineEdit, QTextEdit, QGroupBox, QCheckBox, QComboBox, QProgressBar, QDoubleSpinBox
)

from services.conversion_service import precise_convert, write_outputs
import ezdxf


# -------- Fast layer scan (ezdxf only) --------
def fast_scan_layers(dxf_paths: List[str]) -> List[str]:
    layers: Set[str] = set()
    for path in dxf_paths:
        doc = ezdxf.readfile(path)
        msp = doc.modelspace()
        for e in msp:
            layer = getattr(e.dxf, "layer", "0") or "0"
            layers.add(layer)
            if e.dxftype() == "INSERT":
                try:
                    k = 0
                    for se in e.virtual_entities():
                        sl = getattr(se.dxf, "layer", "0") or "0"
                        layers.add(sl)
                        k += 1
                        if k >= 20:
                            break
                except Exception:
                    pass
    return sorted(layers)


# -------- Simple QThread subclass to run a function --------
class TaskThread(QThread):
    finished = Signal(object)  # result payload
    error = Signal(str)
    log = Signal(str)          # optional progress from worker

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            res = self._fn()
            self.finished.emit(res)
        except Exception as ex:
            tb = traceback.format_exc()
            self.error.emit(f"{ex}\n--- TRACEBACK ---\n{tb}")


# -------- Main Window --------
class MainWindow(QMainWindow):
    # thread-safe log signal (append to QTextEdit on GUI thread)
    logSig = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DXF → GIS Converter (PySide6)")
        self.resize(1120, 720)

        self._files: List[str] = []
        self._running_threads: List[QThread] = []
        self._busy: bool = False  # guard to prevent re-entrance

        root = QWidget(self)
        self.setCentralWidget(root)
        main = QHBoxLayout(root)

        # ===== Left: controls + layers =====
        left = QVBoxLayout()
        main.addLayout(left, 3)

        # --- File group ---
        fileBox = QGroupBox("Files")
        fileL = QVBoxLayout(fileBox)

        # Input DXF
        fileRow = QHBoxLayout()
        self.inPath = QLineEdit()
        self.btnPick = QPushButton("Browse DXF…")
        self.btnPick.clicked.connect(self.on_pick)
        fileRow.addWidget(QLabel("Input DXF:"))
        fileRow.addWidget(self.inPath, 1)
        fileRow.addWidget(self.btnPick)
        fileL.addLayout(fileRow)

        # Output folder / file
        outRow = QHBoxLayout()
        self.outPath = QLineEdit()
        self.btnOut = QPushButton("Choose Output Folder…")
        self.btnOut.clicked.connect(self.on_pick_out)
        outRow.addWidget(QLabel("Output:"))
        outRow.addWidget(self.outPath, 1)
        outRow.addWidget(self.btnOut)
        fileL.addLayout(outRow)

        # Parameters (row 1)
        parmRow1 = QHBoxLayout()
        self.srcEpsg = QLineEdit("3826")
        self.tgtEpsg = QLineEdit("3826")
        self.cmbDriver = QComboBox()
        self.cmbDriver.addItems(["ESRI Shapefile", "GPKG"])
        self.chkOverwrite = QCheckBox("Overwrite existing")
        parmRow1.addWidget(QLabel("Source EPSG:"))
        parmRow1.addWidget(self.srcEpsg)
        parmRow1.addWidget(QLabel("Target EPSG:"))
        parmRow1.addWidget(self.tgtEpsg)
        parmRow1.addWidget(QLabel("Driver:"))
        parmRow1.addWidget(self.cmbDriver)
        parmRow1.addWidget(self.chkOverwrite)
        fileL.addLayout(parmRow1)

        # Parameters (row 2)
        parmRow2 = QHBoxLayout()
        self.chkKeepBlocks = QCheckBox("Keep blocks (merge per INSERT)")
        self.chkKeepBlocks.setChecked(True)  # working default
        parmRow2.addWidget(self.chkKeepBlocks)

        parmRow2.addSpacing(16)
        parmRow2.addWidget(QLabel("Line-merge tolerance (m):"))
        self.spinMergeTol = QDoubleSpinBox()
        self.spinMergeTol.setDecimals(3)
        self.spinMergeTol.setRange(0.0, 1000.0)
        self.spinMergeTol.setSingleStep(0.1)
        self.spinMergeTol.setValue(0.2)  # working default
        parmRow2.addWidget(self.spinMergeTol)
        parmRow2.addStretch(1)
        fileL.addLayout(parmRow2)

        left.addWidget(fileBox)

        # --- Layers group ---
        layerBox = QGroupBox("Layers")
        layerL = QVBoxLayout(layerBox)

        selRow = QHBoxLayout()
        self.btnSelAll = QPushButton("Select All")
        self.btnClear = QPushButton("Clear")
        self.btnSelAll.clicked.connect(self.on_select_all_layers)
        self.btnClear.clicked.connect(self.on_clear_layers)
        selRow.addWidget(QLabel("Choose layers to export:"))
        selRow.addStretch(1)
        selRow.addWidget(self.btnSelAll)
        selRow.addWidget(self.btnClear)
        layerL.addLayout(selRow)

        self.lstLayers = QListWidget()
        self.lstLayers.setSelectionMode(QListWidget.ExtendedSelection)
        layerL.addWidget(self.lstLayers, 1)
        left.addWidget(layerBox, 3)

        # --- Actions ---
        opsRow = QHBoxLayout()
        self.btnAnalyze = QPushButton("Analyze Layers")
        self.btnConvert = QPushButton("Convert")
        self.btnAnalyze.clicked.connect(self.do_analyze)
        self.btnConvert.clicked.connect(self.do_convert)
        opsRow.addWidget(self.btnAnalyze)
        opsRow.addWidget(self.btnConvert)
        left.addLayout(opsRow)

        # --- Progress ---
        prgRow = QHBoxLayout()
        self.prog = QProgressBar()
        self.prog.setRange(0, 0)       # busy indicator
        self.prog.setVisible(False)
        prgRow.addWidget(self.prog)
        left.addLayout(prgRow)

        # ===== Right: log =====
        right = QVBoxLayout()
        main.addLayout(right, 2)

        self.logView = QTextEdit()
        self.logView.setReadOnly(True)
        right.addWidget(QLabel("Log"))
        right.addWidget(self.logView, 1)

        # connect thread-safe logger
        self.logSig.connect(self.logView.append)

    # -------- helpers --------
    def _log(self, msg: str):
        self.logSig.emit(str(msg))

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.prog.setVisible(busy)
        # disable/enable buttons to avoid concurrent runs
        for w in (self.btnAnalyze, self.btnConvert, self.btnPick, self.btnOut, self.btnSelAll, self.btnClear):
            w.setEnabled(not busy)

    def _track(self, th: QThread):
        self._running_threads.append(th)
        def cleanup():
            try:
                self._running_threads.remove(th)
            except ValueError:
                pass
            th.deleteLater()
        th.finished.connect(cleanup)

    def _append_layers(self, layers: List[str]):
        self.lstLayers.clear()
        for name in layers:
            it = QListWidgetItem(name)
            it.setData(Qt.UserRole, name)
            self.lstLayers.addItem(it)
        self._log(f"[layers] Loaded {len(layers)} layers.")

    def _selected_layers(self) -> Optional[List[str]]:
        items = self.lstLayers.selectedItems()
        if not items:
            return None  # None => all
        return [it.data(Qt.UserRole) for it in items]

    # -------- UI events --------
    def on_select_all_layers(self):
        self.lstLayers.selectAll()
        self._log("[layers] Select all.")

    def on_clear_layers(self):
        self.lstLayers.clearSelection()
        self._log("[layers] Cleared selection (no selection means all).")

    def on_pick(self):
        if self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose DXF", "", "AutoCAD DXF (*.dxf)")
        if not path:
            return
        self.inPath.setText(path)
        self._files = [path]
        self._log(f"[file] Input: {path}")

    def on_pick_out(self):
        if self._busy:
            return
        d = QFileDialog.getExistingDirectory(self, "Choose Output Folder")
        if not d:
            return
        self.outPath.setText(d)
        self._log(f"[out] Output: {d}")

    # -------- Actions --------
    def do_analyze(self):
        if self._busy:
            return
        if not self._files:
            QMessageBox.warning(self, "Notice", "Please choose a DXF file first.")
            return

        dxf_path = self._files[0]
        if not os.path.isfile(dxf_path):
            self._log(f"[error] DXF not found: {dxf_path}")
            QMessageBox.critical(self, "Error", f"DXF not found:\n{dxf_path}")
            return

        self._set_busy(True)
        self._log("[analyze] Scanning layers (fast)…")
        t0 = time.perf_counter()

        def task_fn():
            return fast_scan_layers(self._files)

        th = TaskThread(task_fn)
        th.finished.connect(lambda layers: self._analyze_finished(th, layers, t0))
        th.error.connect(lambda msg: self._task_error(th, msg))
        th.start()
        self._track(th)

    def _analyze_finished(self, th: TaskThread, layers: List[str], t0: float):
        if QThread.currentThread() is not th:
            th.wait()
        dt = time.perf_counter() - t0
        self._set_busy(False)
        if not layers:
            self._log("[analyze] No layers found.")
            QMessageBox.information(self, "Done", "No layers found.")
            return
        self._append_layers(layers)
        self._log(f"[analyze] Done. Layers={len(layers)} time={dt:.2f}s")
        QMessageBox.information(self, "Done", f"Found {len(layers)} layers. (time {dt:.2f}s)")

    def do_convert(self):
        if self._busy:
            return
        if not self._files:
            QMessageBox.warning(self, "Notice", "Please choose a DXF file first.")
            return

        dxf_path = self._files[0]
        if not os.path.isfile(dxf_path):
            self._log(f"[error] DXF not found: {dxf_path}")
            QMessageBox.critical(self, "Error", f"DXF not found:\n{dxf_path}")
            return

        outp = self.outPath.text().strip()
        if not outp:
            QMessageBox.warning(self, "Notice", "Please choose an output folder.")
            return
        try:
            pathlib.Path(outp).mkdir(parents=True, exist_ok=True)
        except Exception as ex:
            self._log(f"[error] Cannot create output folder: {outp} → {ex}")
            QMessageBox.critical(self, "Error", f"Cannot create output folder:\n{outp}\n{ex}")
            return

        drv = "GPKG" if (self.cmbDriver.currentText() == "GPKG" or outp.lower().endswith(".gpkg")) else "ESRI Shapefile"

        try:
            src = int(self.srcEpsg.text().strip() or "3826")
        except Exception:
            src = 3826
        try:
            tgt = int(self.tgtEpsg.text().strip()) if self.tgtEpsg.text().strip() else None
        except Exception:
            tgt = None

        target_layers = self._selected_layers()
        if target_layers is None:
            self._log("[convert] No layer selected → using ALL layers.")
        else:
            preview = ", ".join(target_layers[:6])
            suffix = "…" if len(target_layers) > 6 else ""
            self._log(f"[convert] Layers selected ({len(target_layers)}): {preview}{suffix}")

        keep_blocks = self.chkKeepBlocks.isChecked()
        mode = "keep-merge" if keep_blocks else "explode"
        merge_tol = float(self.spinMergeTol.value())

        self._set_busy(True)
        t0 = time.perf_counter()
        self._log(f"[convert] Start… driver={drv} src={src} tgt={tgt} mode={mode} tol={merge_tol}")

        def task_fn():
            t_conv0 = time.perf_counter()
            buckets = precise_convert(
                self._files,
                source_epsg=src,
                target_epsg=tgt,
                include_3d=False,
                bbox_wgs84=None,
                target_layers=target_layers,
                block_mode=mode,
                line_merge_tol=merge_tol,
                fallback_explode_lines=True,
                on_progress=self._log,  # thread-safe
            )
            t_conv = time.perf_counter() - t_conv0

            t_write0 = time.perf_counter()
            written = write_outputs(
                buckets,
                out_path=outp,
                driver=drv,
                overwrite=self.chkOverwrite.isChecked(),
                on_progress=self._log  # thread-safe
            )
            t_write = time.perf_counter() - t_write0

            return {"written": written, "t_convert": t_conv, "t_write": t_write}

        th = TaskThread(task_fn)
        th.finished.connect(lambda res: self._convert_finished(th, res, t0))
        th.error.connect(lambda msg: self._task_error(th, msg))
        th.start()
        self._track(th)

    def _convert_finished(self, th: TaskThread, payload, t0: float):
        if QThread.currentThread() is not th:
            th.wait()
        total = time.perf_counter() - t0
        self._set_busy(False)

        if not payload or not payload.get("written"):
            self._log("[write] No files written (maybe nothing matched the condition).")
            QMessageBox.information(self, "Done", "No files were written.")
            return

        written = payload["written"]
        t_convert = payload.get("t_convert", 0.0)
        t_write = payload.get("t_write", 0.0)

        lines = [f"- {w['layer']} ({w['count']}) → {w['path']}" for w in written]
        summary = "\n".join(lines)
        self._log(f"[convert] Done. buckets={len(written)} convert={t_convert:.2f}s write={t_write:.2f}s total={total:.2f}s")
        self._log("Write complete:\n" + summary)
        QMessageBox.information(self, "Done",
                                f"Successfully wrote {len(written)} layer(s).\n\n"
                                f"Convert: {t_convert:.2f}s\nWrite: {t_write:.2f}s\nTotal: {total:.2f}s\n\n"
                                f"{summary}")

    def _task_error(self, th: TaskThread, msg: str):
        if QThread.currentThread() is not th:
            th.wait()
        self._set_busy(False)
        self._log(f"[ERROR]\n{msg}")
        QMessageBox.critical(self, "Error", msg)

    # graceful shutdown
    def closeEvent(self, event):
        for th in list(self._running_threads):
            try:
                if th.isRunning():
                    th.requestInterruption()
                    th.wait(5000)
            except Exception:
                pass
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.show()
    app.exec()
