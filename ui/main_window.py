# dxf2gis_gui/ui/main_window.py
from __future__ import annotations
import os
import pathlib
import time
import json
from typing import List, Optional, Set, Dict, Tuple, Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFileDialog, QMessageBox,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget, QListWidgetItem,
    QLineEdit, QGroupBox, QCheckBox, QComboBox, QProgressBar, QDoubleSpinBox
)

from services.conversion_service import precise_convert, write_outputs
import ezdxf

# Optional DWG support (external converters only; optional)
from services.dwg_support import dwg_to_temp_dxf_auto, detect_dwg_converter

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


# -------- helpers for bbox from GeoJSON dict --------
def _minmax_bbox_of_coords(coords: Any, cur: Optional[Tuple[float,float,float,float]]) -> Tuple[float,float,float,float]:
    # coords is nested lists of [lon,lat,(z)]
    s, w, n, e = cur if cur else (  90.0,  180.0, -90.0, -180.0)
    if isinstance(coords, (list, tuple)):
        if len(coords) >= 2 and isinstance(coords[0], (int,float)) and isinstance(coords[1], (int,float)):
            lon = float(coords[0]); lat = float(coords[1])
            if lat < s: s = lat
            if lat > n: n = lat
            if lon < w: w = lon
            if lon > e: e = lon
        else:
            for c in coords:
                s, w, n, e = _minmax_bbox_of_coords(c, (s,w,n,e))
    return (s, w, n, e)

def compute_geojson_dict_bbox(layers: Dict[str, Any]) -> Optional[Tuple[float,float,float,float]]:
    bbox: Optional[Tuple[float,float,float,float]] = None
    for _name, fc in layers.items():
        if not isinstance(fc, dict):
            continue
        feats = fc.get("features") or []
        for f in feats:
            geom = (f or {}).get("geometry") or {}
            coords = geom.get("coordinates")
            if coords is None:
                continue
            bbox = _minmax_bbox_of_coords(coords, bbox)
    return bbox

def compute_geojson_bbox_for_selection(layers: Dict[str, Any], selected_layer_names: Optional[List[str]]) -> Optional[Tuple[float,float,float,float]]:
    """selected_layer_names are raw layer names (without ' (GEOM)').
       We include any payload key that startswith '<name> (' """
    if not layers:
        return None
    if not selected_layer_names:
        return compute_geojson_dict_bbox(layers)
    keys: List[str] = []
    sel_set = set(selected_layer_names)
    for key in layers.keys():
        base = key.split(" (", 1)[0]
        if base in sel_set:
            keys.append(key)
    subset = {k: layers[k] for k in keys}
    return compute_geojson_dict_bbox(subset)


# -------- Main Window (DXF-first; optional DWG via external converter) --------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DXF → GIS Converter (PySide6 + Leaflet)")
        self.resize(1280, 780)

        self._files: List[str] = []
        self._busy: bool = False
        self._temp_files: List[str] = []  # track temp DXFs generated from DWG
        self._threads: List[QThread] = []  # keep references to running threads
        self._last_map_payload: Dict[str, Any] = {}  # store last shown GeoJSON dict

        # Detect optional DWG converter
        self._dwg_converter = detect_dwg_converter()  # 'oda' | 'libredwg' | ''
        if self._dwg_converter == "oda":
            self._dwg_label = "ODA File Converter detected"
        elif self._dwg_converter == "libredwg":
            self._dwg_label = "LibreDWG (dwg2dxf) detected"
        else:
            self._dwg_label = "No DWG converter found (DXF only)"

        root = QWidget(self)
        self.setCentralWidget(root)
        main = QHBoxLayout(root)

        # ===== Left: controls + layers =====
        left = QVBoxLayout()
        main.addLayout(left, 3)

        # --- File group ---
        fileBox = QGroupBox("Files")
        fileL = QVBoxLayout(fileBox)

        # Input CAD
        fileRow = QHBoxLayout()
        self.inPath = QLineEdit()
        self.btnPick = QPushButton("Browse CAD…")
        self.btnPick.clicked.connect(self.on_pick)
        fileRow.addWidget(QLabel("Input (DXF/DWG):"))
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
        self.chkKeepBlocks.setChecked(True)
        parmRow2.addWidget(self.chkKeepBlocks)

        parmRow2.addSpacing(12)
        parmRow2.addWidget(QLabel("Line-merge tolerance (m):"))
        self.spinMergeTol = QDoubleSpinBox()
        self.spinMergeTol.setDecimals(3)
        self.spinMergeTol.setRange(0.0, 1000.0)
        self.spinMergeTol.setSingleStep(0.1)
        self.spinMergeTol.setValue(0.2)
        parmRow2.addWidget(self.spinMergeTol)

        parmRow2.addSpacing(12)
        # Optional DWG toggle (only meaningful if converter is present)
        self.chkEnableDWG = QCheckBox("Enable DWG (if available)")
        self.chkEnableDWG.setChecked(False)  # default OFF to keep zero-install DXF-only
        self.chkEnableDWG.setToolTip(self._dwg_label)
        if not self._dwg_converter:
            self.chkEnableDWG.setEnabled(False)
        parmRow2.addWidget(self.chkEnableDWG)

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

        # --- Actions (Analyze, Show in Map, Zoom to selection, Convert) ---
        opsRow = QHBoxLayout()
        self.btnAnalyze = QPushButton("Analyze Layers")
        self.btnShowMap = QPushButton("Show in Map")
        self.btnZoomSel = QPushButton("Zoom to selection")
        self.btnConvert = QPushButton("Convert")
        self.btnAnalyze.clicked.connect(self.do_analyze)
        self.btnShowMap.clicked.connect(self.do_show_in_map)
        self.btnZoomSel.clicked.connect(self.do_zoom_selection)
        self.btnConvert.clicked.connect(self.do_convert)
        opsRow.addWidget(self.btnAnalyze)
        opsRow.addWidget(self.btnShowMap)
        opsRow.addWidget(self.btnZoomSel)
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

    # -------- thread helper --------
    def _run_in_thread(self, fn, on_finished, on_error):
        th = TaskThread(fn)
        self._threads.append(th)
        def _fin(res):
            try:
                on_finished(th, res)
            finally:
                if th in self._threads:
                    self._threads.remove(th)
                th.deleteLater()
        def _err(msg):
            try:
                on_error(th, msg)
            finally:
                if th in self._threads:
                    self._threads.remove(th)
                th.deleteLater()
        th.finished.connect(_fin)
        th.error.connect(_err)
        th.start()
        return th

    # -------- helpers --------
    def _set_busy(self, busy: bool):
        self._busy = busy
        self.prog.setVisible(busy)
        for w in (self.btnAnalyze, self.btnConvert, self.btnPick, self.btnOut,
                  self.btnSelAll, self.btnClear, self.btnShowMap, self.btnZoomSel, self.chkEnableDWG):
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
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose CAD file", "",
            "CAD Files (*.dxf *.dwg);;DXF Files (*.dxf);;DWG Files (*.dwg)"
        )
        if not path:
            return

        ext = os.path.splitext(path)[1].lower()
        if ext == ".dwg":
            if not self.chkEnableDWG.isChecked() or not self._dwg_converter:
                QMessageBox.information(
                    self, "DWG disabled",
                    "This build runs DXF-only by default (no external tools required).\n\n"
                    "To use DWG, install ODA File Converter or LibreDWG (dwg2dxf), "
                    "then enable 'Enable DWG'."
                )
                return

            # Background DWG → temp DXF conversion
            self._set_busy(True)
            prefer = "auto"  # or 'oda' / 'libredwg'
            def task_fn():
                return dwg_to_temp_dxf_auto(path, prefer=prefer, dxf_version="ACAD2013")

            def _done(_th, temp_dxf):
                self._set_busy(False)
                if not temp_dxf or not os.path.isfile(temp_dxf):
                    QMessageBox.critical(self, "Error", "DWG conversion failed (no DXF produced).")
                    return
                self._temp_files.append(temp_dxf)
                self._files = [temp_dxf]
                self.inPath.setText(f"{path}  (via temp DXF)")
                QMessageBox.information(self, "Info", "DWG converted to temporary DXF. You can Analyze / Show / Convert now.")

            def _err(_th, msg):
                self._set_busy(False)
                QMessageBox.critical(self, "Error", msg)

            self._run_in_thread(task_fn, _done, _err)
            return

        # default DXF path
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
            QMessageBox.warning(self, "Notice", "Please choose a DXF/DWG file first.")
            return

        dxf_path = self._files[0]
        if not os.path.isfile(dxf_path):
            QMessageBox.critical(self, "Error", f"Input file not found:\n{dxf_path}")
            return

        self._set_busy(True)
        t0 = time.perf_counter()

        def task_fn():
            return fast_scan_layers(self._files)

        def _done(_th, layers: List[str]):
            dt = time.perf_counter() - t0
            self._set_busy(False)
            if not layers:
                QMessageBox.information(self, "Done", "No layers found.")
                return
            self._append_layers(layers)
            QMessageBox.information(self, "Done", f"Found {len(layers)} layers. (time {dt:.2f}s)")

        def _err(_th, msg: str):
            self._set_busy(False)
            QMessageBox.critical(self, "Error", msg)

        self._run_in_thread(task_fn, _done, _err)

    def do_show_in_map(self):
        if self._busy:
            return
        if not self._files:
            QMessageBox.warning(self, "Notice", "Please choose a DXF/DWG file first.")
            return

        try:
            src = int(self.srcEpsg.text().strip() or "3826")
        except Exception:
            src = 3826

        target_layers = self._selected_layers()  # None => all
        keep_blocks = self.chkKeepBlocks.isChecked()
        mode = "keep-merge" if keep_blocks else "explode"

        self._set_busy(True)

        def task_fn():
            buckets = precise_convert(
                self._files,
                source_epsg=src,
                target_epsg=4326,      # web map
                include_3d=False,
                bbox_wgs84=None,
                target_layers=target_layers,
                block_mode=mode,
                line_merge_tol=0.5,    # lighter for preview
                fallback_explode_lines=True,
                on_progress=None,
            )
            result: Dict[str, dict] = {}
            MAX_FEAT = 20000
            for (layer, geom), gdf in buckets.items():
                if gdf.empty:
                    continue
                if len(gdf) > MAX_FEAT:
                    gdf = gdf.iloc[:MAX_FEAT].copy()
                try:
                    if str(getattr(gdf, "crs", None)).upper() not in ("EPSG:4326",):
                        gdf = gdf.to_crs(4326)
                except Exception:
                    pass
                try:
                    gj_obj = json.loads(gdf.to_json(drop_id=True))
                except Exception:
                    continue
                key = f"{layer} ({geom})"
                result[key] = gj_obj
            # compute bbox in WGS84 for auto-zoom
            bbox = compute_geojson_dict_bbox(result)  # (south, west, north, east) or None
            return {"layers": result, "bbox": bbox}

        def _done(_th, payload: Dict[str, Any]):
            self._set_busy(False)
            if not payload or not isinstance(payload, dict):
                QMessageBox.information(self, "Map", "No features to show.")
                return
            layers = payload.get("layers") or {}
            bbox = payload.get("bbox")
            if not layers:
                QMessageBox.information(self, "Map", "No features to show.")
                return
            self._last_map_payload = layers  # store for "Zoom to selection"
            self.map.show_geojson(layers)
            if bbox:
                self.map.fit_bounds(tuple(bbox))  # (south, west, north, east)

        def _err(_th, msg: str):
            self._set_busy(False)
            QMessageBox.critical(self, "Error", msg)

        self._run_in_thread(task_fn, _done, _err)

    def do_zoom_selection(self):
        """Zoom only to selected layers (using already shown payload)."""
        if not self._last_map_payload:
            QMessageBox.information(self, "Zoom", "Map has no layers yet. Use 'Show in Map' first.")
            return
        selected = self._selected_layers()
        bbox = compute_geojson_bbox_for_selection(self._last_map_payload, selected)
        if bbox:
            self.map.fit_bounds(bbox)
        else:
            QMessageBox.information(self, "Zoom", "No geometry found for the current selection.")

    def do_convert(self):
        if self._busy:
            return
        if not self._files:
            QMessageBox.warning(self, "Notice", "Please choose a DXF/DWG file first.")
            return

        dxf_path = self._files[0]
        if not os.path.isfile(dxf_path):
            QMessageBox.critical(self, "Error", f"Input file not found:\n{dxf_path}")
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
                on_progress=None,
            )
            written = write_outputs(
                buckets,
                out_path=outp,
                driver=drv,
                overwrite=self.chkOverwrite.isChecked(),
                on_progress=None,
            )
            return written

        def _done(_th, written):
            self._set_busy(False)
            dt = time.perf_counter() - t0
            if not written:
                QMessageBox.information(self, "Done", "No files were written.")
                return
            lines = [f"- {w['layer']} ({w['count']}) → {w['path']}" for w in written]
            summary = "\n".join(lines)
            QMessageBox.information(self, "Done", f"Successfully wrote {len(written)} layer(s) in {dt:.2f}s.\n\n{summary}")

        def _err(_th, msg: str):
            self._set_busy(False)
            QMessageBox.critical(self, "Error", msg)

        self._run_in_thread(task_fn, _done, _err)

    def closeEvent(self, event):
        # cleanup temp DXFs created from DWG
        for p in list(self._temp_files):
            try:
                if os.path.isfile(p):
                    os.remove(p)
                folder = os.path.dirname(p)
                try:
                    os.rmdir(folder)
                except Exception:
                    pass
            except Exception:
                pass
        self._temp_files.clear()

        # ensure no threads leak
        for th in list(self._threads):
            try:
                if th.isRunning():
                    th.quit()
                    th.wait(2000)
            except Exception:
                pass
        self._threads.clear()

        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.show()
    app.exec()
