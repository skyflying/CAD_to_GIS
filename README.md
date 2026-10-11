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
pip install pyside6 geopandas shapely fiona pyproj pandas ezdxf
```

Run the app:
```bash
python dxf2gis_gui/app.py
```

### From Executable (Windows)

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
