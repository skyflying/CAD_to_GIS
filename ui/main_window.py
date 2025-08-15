# dxf2gis_gui/ui/main_window.py
from __future__ import annotations
import os
import pathlib
import time
import json
from typing import List, Optional, Set, Dict

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFileDialog, QMessageBox,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget, QListWidgetItem,
    QLineEdit, QGroupBox, QCheckBox, QComboBox, QProgressBar, QDoubleSpinBox
)

from services.conversion_service import precise_convert, write_outputs
import ezdxf

# Map widget (Leaflet in QWebEngine)
from ui.map_view import MapView


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

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            res = self._fn()
            self.finished.emit(res)
        except Exception as ex:
            import traceback
            tb = traceback.format_exc()
            self.error.emit(f"{ex}\n--- TRACEBACK ---\n{tb}")


# -------- Main Window (no log panel; right = map) --------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DXF → GIS Converter (PySide6 + Leaflet)")
        self.resize(1200, 760)

        self._files: List[str] = []
        self._running_threads: List[QThread] = []
        self._busy: bool = False

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

        # --- Actions (Analyze, Show in Map, Convert) ---
        opsRow = QHBoxLayout()
        self.btnAnalyze = QPushButton("Analyze Layers")
        self.btnShowMap = QPushButton("Show in Map")
        self.btnConvert = QPushButton("Convert")
        self.btnAnalyze.clicked.connect(self.do_analyze)
        self.btnShowMap.clicked.connect(self.do_show_in_map)
        self.btnConvert.clicked.connect(self.do_convert)
        opsRow.addWidget(self.btnAnalyze)
        opsRow.addWidget(self.btnShowMap)
        opsRow.addWidget(self.btnConvert)
        left.addLayout(opsRow)

        # --- Progress ---
        prgRow = QHBoxLayout()
        self.prog = QProgressBar()
        self.prog.setRange(0, 0)       # busy indicator
        self.prog.setVisible(False)
        prgRow.addWidget(self.prog)
        left.addLayout(prgRow)

        # ===== Right: map (Leaflet) =====
        self.map = MapView(self)
        main.addWidget(self.map, 5)
        self.map.load_empty()

    # -------- helpers --------
    def _set_busy(self, busy: bool):
        self._busy = busy
        self.prog.setVisible(busy)
        # disable/enable buttons to avoid concurrent runs
        for w in (self.btnAnalyze, self.btnConvert, self.btnPick, self.btnOut, self.btnSelAll, self.btnClear, self.btnShowMap):
            w.setEnabled(not busy)

    def _append_layers(self, layers: List[str]):
        self.lstLayers.clear()
        for name in layers:
            it = QListWidgetItem(name)
            it.setData(Qt.UserRole, name)
            self.lstLayers.addItem(it)

    def _selected_layers(self) -> Optional[List[str]]:
        items = self.lstLayers.selectedItems()
        if not items:
            return None  # None => all
        return [it.data(Qt.UserRole) for it in items]

    # -------- UI events --------
    def on_select_all_layers(self):
        self.lstLayers.selectAll()

    def on_clear_layers(self):
        self.lstLayers.clearSelection()

    def on_pick(self):
        if self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose DXF", "", "AutoCAD DXF (*.dxf)")
        if not path:
            return
        self.inPath.setText(path)
        self._files = [path]
        QMessageBox.information(self, "Info", f"Input: {path}")

    def on_pick_out(self):
        if self._busy:
            return
        d = QFileDialog.getExistingDirectory(self, "Choose Output Folder")
        if not d:
            return
        self.outPath.setText(d)
        QMessageBox.information(self, "Info", f"Output: {d}")

    # -------- Actions --------
    def do_analyze(self):
        if self._busy:
            return
        if not self._files:
            QMessageBox.warning(self, "Notice", "Please choose a DXF file first.")
            return

        dxf_path = self._files[0]
        if not os.path.isfile(dxf_path):
            QMessageBox.critical(self, "Error", f"DXF not found:\n{dxf_path}")
            return

        self._set_busy(True)
        t0 = time.perf_counter()

        def task_fn():
            return fast_scan_layers(self._files)

        th = TaskThread(task_fn)
        th.finished.connect(lambda layers: self._analyze_finished(th, layers, t0))
        th.error.connect(lambda msg: self._task_error(th, msg))
        th.start()

    def _analyze_finished(self, th: TaskThread, layers: List[str], t0: float):
        if QThread.currentThread() is not th:
            th.wait()
        dt = time.perf_counter() - t0
        self._set_busy(False)
        if not layers:
            QMessageBox.information(self, "Done", "No layers found.")
            return
        self._append_layers(layers)
        QMessageBox.information(self, "Done", f"Found {len(layers)} layers. (time {dt:.2f}s)")

    def do_show_in_map(self):
        """Build a light-weight GeoJSON preview for selected (or all) layers and render on map."""
        if self._busy:
            return
        if not self._files:
            QMessageBox.warning(self, "Notice", "Please choose a DXF file first.")
            return

        try:
            src = int(self.srcEpsg.text().strip() or "3826")
        except Exception:
            src = 3826

        target_layers = self._selected_layers()  # None => all
        keep_blocks = self.chkKeepBlocks.isChecked()
        mode = "keep-merge" if keep_blocks else "explode"

        # Build preview in background (target_crs = 4326 for web map)
        self._set_busy(True)

        def task_fn():
            # Faster settings for preview: target_epsg=4326, lighter tolerance
            buckets = precise_convert(
                self._files,
                source_epsg=src,
                target_epsg=4326,
                include_3d=False,
                bbox_wgs84=None,
                target_layers=target_layers,
                block_mode="explode" if mode == "explode" else "keep-merge",
                line_merge_tol=0.5,               # looser tolerance for preview
                fallback_explode_lines=True,
                on_progress=None,                 # no logging to UI
            )
            # Convert each bucket to GeoJSON dict (limit features per layer to keep the map fast)
            # buckets: Dict[(layer, geom), GeoDataFrame]
            result: Dict[str, dict] = {}
            MAX_FEAT = 1000  # per bucket cap; adjust if needed
            for (layer, geom), gdf in buckets.items():
                if gdf.empty:
                    continue
                # Limit features
                if len(gdf) > MAX_FEAT:
                    gdf = gdf.iloc[:MAX_FEAT].copy()
                # Ensure CRS is WGS84
                try:
                    if str(getattr(gdf, "crs", None)).upper() not in ("EPSG:4326", "EPSG:4326"):
                        gdf = gdf.to_crs(4326)
                except Exception:
                    pass
                gj_text = gdf.to_json(drop_id=True)
                try:
                    gj_obj = json.loads(gj_text)
                except Exception:
                    continue
                key = f"{layer} ({geom})"
                result[key] = gj_obj
            return result

        th = TaskThread(task_fn)
        th.finished.connect(lambda payload: self._show_map_finished(th, payload))
        th.error.connect(lambda msg: self._task_error(th, msg))
        th.start()

    def _show_map_finished(self, th: TaskThread, payload: Dict[str, dict]):
        if QThread.currentThread() is not th:
            th.wait()
        self._set_busy(False)
        if not payload:
            QMessageBox.information(self, "Map", "No features to show.")
            return
        # Send to map
        self.map.show_geojson(payload)

    def do_convert(self):
        if self._busy:
            return
        if not self._files:
            QMessageBox.warning(self, "Notice", "Please choose a DXF file first.")
            return

        dxf_path = self._files[0]
        if not os.path.isfile(dxf_path):
            QMessageBox.critical(self, "Error", f"DXF not found:\n{dxf_path}")
            return

        outp = self.outPath.text().strip()
        if not outp:
            QMessageBox.warning(self, "Notice", "Please choose an output folder.")
            return
        try:
            pathlib.Path(outp).mkdir(parents=True, exist_ok=True)
        except Exception as ex:
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
        keep_blocks = self.chkKeepBlocks.isChecked()
        mode = "keep-merge" if keep_blocks else "explode"
        merge_tol = float(self.spinMergeTol.value())

        self._set_busy(True)
        t0 = time.perf_counter()

        def task_fn():
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
                on_progress=None,  # no UI logging
            )
            written = write_outputs(
                buckets,
                out_path=outp,
                driver=drv,
                overwrite=self.chkOverwrite.isChecked(),
                on_progress=None,  # no UI logging
            )
            return written

        th = TaskThread(task_fn)
        th.finished.connect(lambda written: self._convert_finished(th, written, t0))
        th.error.connect(lambda msg: self._task_error(th, msg))
        th.start()

    def _convert_finished(self, th: TaskThread, written, t0: float):
        if QThread.currentThread() is not th:
            th.wait()
        self._set_busy(False)
        dt = time.perf_counter() - t0
        if not written:
            QMessageBox.information(self, "Done", "No files were written.")
            return
        lines = [f"- {w['layer']} ({w['count']}) → {w['path']}" for w in written]
        summary = "\n".join(lines)
        QMessageBox.information(self, "Done", f"Successfully wrote {len(written)} layer(s) in {dt:.2f}s.\n\n{summary}")

    def _task_error(self, th: TaskThread, msg: str):
        if QThread.currentThread() is not th:
            th.wait()
        self._set_busy(False)
        QMessageBox.critical(self, "Error", msg)


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.show()
    app.exec()
