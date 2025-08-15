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
