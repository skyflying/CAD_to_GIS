# CAD to GIS
It is a Python-based desktop application for converting CAD DXF files into GIS formats (Shapefile, GeoPackage).
It provides an easy-to-use graphical interface for scanning layers, selecting which layers to export, and handling CAD blocks either exploded or preserved (merged) before conversion.

---

## ✨ Features

- **Fast layer scanning** with entity count per layer  
- **Layer selection UI** with _Select All_ and _Clear All_ buttons  
- **Block handling modes**:
  - `explode` – break blocks into individual entities  
  - `keep-merge` – preserve blocks and merge into single geometries  
- **Precise coordinate transformation** with source & target EPSG codes  
- **Supports multi-layer output** – each layer saved as a separate Shapefile or GeoPackage layer  
- **Detailed logging** with entity counts, progress, and final output list  
- **Runs as standalone executable** (via PyInstaller)  

---

## 📦 Installation

You can run the app from source or download a packaged executable.

### 1. From Source

**Requirements** (Python 3.9+ recommended):

```bash
pip install -r requirements.txt
```

Run the app:
```bash
python app.py
```

### From Executable (Windows)

Build it yourself with PyInstaller: `pip install pyinstaller` then `python tools/build_exe.py` → `dist/CAD_to_GIS/CAD_to_GIS.exe`.


## 📐 DWG support (no ODA required)

DWG files are opened through an automatic fallback chain:

| Order | Reader | Install | Notes |
|---|---|---|---|
| 1 | **ODA File Converter** | user installs it ([download](https://www.opendesign.com/guestfiles/oda_file_converter)) | Best quality. ODA states non-members may use it for **non-commercial purposes only** — check the licence for company use. |
| 2 | **LibreDWG** (`dwg2dxf`) | one-click **Download LibreDWG** (Windows, ~12 MB, GPL-3.0) or your package manager | Stored in `%APPDATA%\CAD_to_GIS\libredwg`. Newer entity types may be less complete than ODA. |
| 3 | **GDAL CAD driver** | built into GDAL/QGIS | **DWG R2000 (AC1015) only**; blocks are not merged, hatches/splines may be missing. |
| — | none | — | A dialog explains the options (download LibreDWG / set ODA path / save as DXF). |

The UI shows which reader is active (**DWG reader** status) and offers *Set ODA path…*, *Download LibreDWG*, *ODA download page* and *Re-detect*.
Tool paths are remembered in `%APPDATA%\CAD_to_GIS\dwg_tools.json` (Linux/macOS: `~/.config/CAD_to_GIS/`), shared by the standalone app and the QGIS plugin.
Behind a proxy/firewall, download `libredwg-<ver>-win64.zip` manually from <https://github.com/LibreDWG/libredwg/releases>, unzip it into `%APPDATA%\CAD_to_GIS\libredwg\` and press *Re-detect*.

## 🚀 Usage

1. Open a DXF file
- Click "Browse" to select a DXF file.
- Layers will be scanned and listed in the left panel.
2. Select output location
- Choose a folder for Shapefile output (.shp) or a GeoPackage file (.gpkg).
3. Choose output driver
- ESRI Shapefile or GPKG.
4. Set coordinate system
- Enter Source EPSG (default 3826) and optionally a Target EPSG.
5. Select block handling mode
- explode or keep-merge.
6. Select layers
- Pick specific layers or use Select All / Clear All buttons.
7. Convert
-  Click "Convert" to start. Progress and logs will be shown in the right panel.

![image](https://github.com/skyflying/CAD_to_GIS/blob/main/Image.jpg)


---

## 📝 Changelog

### 1.5.0 – 2026-10-11
- DWG without ODA: ODA → LibreDWG (optional one-click download, Windows) → GDAL CAD driver (DWG R2000) → guidance dialog; DWG reader status row with *Set ODA path / Download LibreDWG / ODA download page / Re-detect*
- Fixed ODA File Converter call (wrong argument order; converted the whole input folder)
- Fixed crash in *Show in Map* (`QThread` destroyed while running: the custom `finished` signal shadowed `QThread.finished`)
- GPKG output no longer creates a *folder* named `xxx.gpkg`
- Line-merge tolerance from the UI was ignored – fixed
- Target EPSG and WGS84 bbox now applied in the ezdxf pipeline; OGR pipeline reprojects to EPSG:4326 too
- GPKG layers named `<Layer>_<GEOM>` (LINE/POLYGON/POINT of one CAD layer no longer overwrite each other)
- Chinese / non-ASCII layer names kept in output names (no more `__LINE.shp` collisions)
- Nested blocks, layer-0 inheritance, hatch polyline boundaries and holes, arcs across 0°, block attributes (`ATT_*`)
- OGR fallback: one feature per INSERT, block attributes by handle, UTF-8 for DXF R2007+
- Map preview works offline (bundled Leaflet inlined)
- Layer scan covers nested blocks
- `tools/cli_convert.py` accepts DWG; `app.py` runs from any folder and exits cleanly (explicit window teardown)
- Added `requirements.txt` and `tools/build_exe.py` (PyInstaller one-folder build)
- Startup check lists missing Python packages with the exact pip command; ezdxf is optional when GDAL (osgeo) is installed
