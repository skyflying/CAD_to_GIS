# -*- coding: utf-8 -*-
"""
DWG support shared by CAD_to_GIS (standalone) and CAD_to_GIS_QGIS_Plugin.

Reading order (automatic):
  1. ODA File Converter   DWG -> temp DXF   (best quality; must be installed by the user)
  2. LibreDWG dwg2dxf     DWG -> temp DXF   (GPL; can be downloaded on demand, Windows)
  3. GDAL "CAD" driver    reads DWG directly (built into GDAL/QGIS; DWG R2000 only)
  4. none -> DwgUnavailableError with guidance text for the UI

Settings (user-chosen tool paths) are stored in a small JSON file so both apps
share them and this module stays Qt-free:
  Windows: %APPDATA%\\CAD_to_GIS\\dwg_tools.json     others: ~/.config/CAD_to_GIS/dwg_tools.json
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

ProgressCB = Optional[Callable[[str], None]]

ODA_DOWNLOAD_URL = "https://www.opendesign.com/guestfiles/oda_file_converter"
LIBREDWG_RELEASES_URL = "https://github.com/LibreDWG/libredwg/releases"
LIBREDWG_FALLBACK_VERSION = "0.14"     # used if "latest" cannot be resolved

# DWG header magic -> readable version
DWG_VERSIONS = {
    "AC1009": "R11/R12", "AC1012": "R13", "AC1014": "R14", "AC1015": "R2000",
    "AC1018": "R2004", "AC1021": "R2007", "AC1024": "R2010", "AC1027": "R2013",
    "AC1032": "R2018",
}
GDAL_CAD_VERSIONS = {"AC1015"}         # libopencad: DWG R2000 only


class DwgUnavailableError(RuntimeError):
    """No converter could read this DWG. `.guidance` is a user-facing explanation."""

    def __init__(self, message: str, guidance: str = "", version: str = ""):
        super().__init__(message)
        self.guidance = guidance or message
        self.version = version


def _say(cb: ProgressCB, msg: str):
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------

def config_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, "CAD_to_GIS")
    os.makedirs(d, exist_ok=True)
    return d


def _config_file() -> str:
    return os.path.join(config_dir(), "dwg_tools.json")


def load_config() -> Dict[str, str]:
    try:
        with open(_config_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg: Dict[str, str]) -> None:
    try:
        with open(_config_file(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def set_oda_path(path: Optional[str]) -> None:
    """Remember a user-selected ODAFileConverter executable (None/'' clears it)."""
    cfg = load_config()
    if path:
        cfg["oda_path"] = os.path.abspath(path)
    else:
        cfg.pop("oda_path", None)
    save_config(cfg)


def set_libredwg_path(path: Optional[str]) -> None:
    cfg = load_config()
    if path:
        cfg["libredwg_path"] = os.path.abspath(path)
    else:
        cfg.pop("libredwg_path", None)
    save_config(cfg)


# ---------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------

def _is_file(p: Optional[str]) -> bool:
    return bool(p) and os.path.isfile(p)  # type: ignore[arg-type]


def _which(cmd: str) -> Optional[str]:
    p = shutil.which(cmd)
    if p:
        return p
    if os.name == "nt" and not cmd.lower().endswith(".exe"):
        return shutil.which(cmd + ".exe")
    return None


def find_oda() -> Optional[str]:
    # 1) user setting  2) env  3) PATH  4) common install roots
    oda = load_config().get("oda_path")
    if _is_file(oda):
        return os.path.abspath(oda)
    oda = os.environ.get("ODA_CONVERTER")
    if _is_file(oda):
        return os.path.abspath(oda)
    for name in ("ODAFileConverter.exe", "ODAFileConverter_x64.exe", "ODAFileConverter"):
        p = _which(name)
        if _is_file(p):
            return os.path.abspath(p)
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
        hits: List[str] = []
        for root in roots:
            for sub in ("ODA", "Open Design Alliance"):
                hits += glob.glob(os.path.join(root, sub, "*", "ODAFileConverter.exe"))
                hits += glob.glob(os.path.join(root, sub, "ODAFileConverter.exe"))
        if hits:
            return os.path.abspath(sorted(hits)[-1])      # newest version folder
    elif sys.platform == "darwin":
        p = "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"
        if os.path.isfile(p):
            return p
    else:
        for p in ("/usr/bin/ODAFileConverter", "/usr/local/bin/ODAFileConverter", "/opt/bin/ODAFileConverter"):
            if os.path.isfile(p):
                return p
    return None


def _managed_libredwg_dir() -> str:
    return os.path.join(config_dir(), "libredwg")


def find_libredwg() -> Optional[str]:
    # 1) user setting  2) copy downloaded by download_libredwg()  3) PATH
    p = load_config().get("libredwg_path")
    if _is_file(p):
        return os.path.abspath(p)
    exe = "dwg2dxf.exe" if os.name == "nt" else "dwg2dxf"
    hits = glob.glob(os.path.join(_managed_libredwg_dir(), "**", exe), recursive=True)
    if hits:
        return os.path.abspath(sorted(hits)[-1])
    p = _which("dwg2dxf")
    return os.path.abspath(p) if _is_file(p) else None


def gdal_cad_available() -> bool:
    try:
        from osgeo import ogr
        return ogr.GetDriverByName("CAD") is not None
    except Exception:
        return False


def gdal_cad_layers(path: str) -> List[str]:
    """Layer names of a DWG read directly with the GDAL CAD driver (R2000 only)."""
    try:
        from osgeo import gdal
        ds = gdal.OpenEx(path, gdal.OF_VECTOR, allowed_drivers=["CAD"])
    except Exception:
        return []
    if ds is None:
        return []
    names = sorted({ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())})
    ds = None
    return names


def dwg_version(path: str) -> Tuple[str, str]:
    """('AC1032', 'R2018') from the file header; ('', 'unknown') if not a DWG."""
    try:
        with open(path, "rb") as f:
            magic = f.read(6).decode("ascii", "ignore")
        if magic.startswith("AC"):
            return magic, DWG_VERSIONS.get(magic, magic)
    except Exception:
        pass
    return "", "unknown"


def detect_status() -> Dict[str, object]:
    """Snapshot for the UI: which DWG back-ends exist, plus a one-line summary."""
    oda, lib, cad = find_oda(), find_libredwg(), gdal_cad_available()
    if oda:
        level, summary = "ok", "✔ ODA File Converter"
    elif lib:
        level, summary = "ok", "✔ LibreDWG (dwg2dxf)"
    elif cad:
        level, summary = "limited", "⚠ GDAL only — DWG R2000 files only"
    else:
        level, summary = "none", "✖ No DWG reader — DXF only"
    extras = [n for n, ok in (("LibreDWG", lib and oda), ("GDAL-CAD", cad and (oda or lib))) if ok]
    if extras:
        summary += "  (+ " + ", ".join(extras) + ")"
    return {"oda": oda, "libredwg": lib, "gdal_cad": cad, "level": level, "summary": summary}


def detect_dwg_converter() -> str:
    """Backward compatible: 'oda' | 'libredwg' | 'gdal' | ''."""
    s = detect_status()
    if s["oda"]:
        return "oda"
    if s["libredwg"]:
        return "libredwg"
    return "gdal" if s["gdal_cad"] else ""


# ---------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------

def _run(cmd: List[str], timeout: int = 900) -> subprocess.CompletedProcess:
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW (ODA pops up a window)
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout, **kw)


def convert_with_oda(dwg_path: str, out_dir: str, dxf_version: str = "ACAD2013") -> str:
    """ODAFileConverter <in_dir> <out_dir> <version> <DXF|DWG> <recurse> <audit> [filter]

    The old call passed "*" in the <version> slot (wrong argument order) and used the
    DWG's own folder as input, i.e. it tried to convert every file in that folder.
    Now the DWG is copied alone into a private input folder.
    """
    exe = find_oda()
    if not exe:
        raise RuntimeError("ODA File Converter not found.")
    in_dir = tempfile.mkdtemp(prefix="oda_in_")
    try:
        src = os.path.join(in_dir, os.path.basename(dwg_path))
        shutil.copy2(dwg_path, src)
        res = _run([exe, in_dir, out_dir, dxf_version or "ACAD2013", "DXF", "0", "1",
                    os.path.basename(dwg_path)])
        stem = os.path.splitext(os.path.basename(dwg_path))[0].lower()
        found = [p for p in glob.glob(os.path.join(out_dir, "**", "*.dxf"), recursive=True)
                 if os.path.splitext(os.path.basename(p))[0].lower() == stem]
        if not found:
            raise RuntimeError(f"ODA converter produced no DXF (code {res.returncode}).\n{res.stdout[-2000:]}")
        return found[0]
    finally:
        shutil.rmtree(in_dir, ignore_errors=True)


def convert_with_libredwg(dwg_path: str, out_dir: str) -> str:
    exe = find_libredwg()
    if not exe:
        raise RuntimeError("LibreDWG 'dwg2dxf' not found.")
    out_path = os.path.join(out_dir, os.path.splitext(os.path.basename(dwg_path))[0] + ".dxf")
    res = _run([exe, "-y", "-o", out_path, os.path.abspath(dwg_path)],)
    # dwg2dxf often returns non-zero for recoverable warnings: judge by the output file
    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"dwg2dxf failed (code {res.returncode}).\n{res.stdout[-2000:]}")
    return out_path


def dwg_to_temp_dxf_auto(dwg_path: str, prefer: str = "auto", dxf_version: str = "ACAD2013",
                         on_progress: ProgressCB = None) -> str:
    """DWG -> temporary DXF via ODA or LibreDWG. Caller deletes os.path.dirname(result).

    Kept for backward compatibility; new code should call open_dwg().
    """
    if not os.path.isfile(dwg_path):
        raise FileNotFoundError(f"DWG not found: {dwg_path}")
    order = {"oda": ["oda", "libredwg"], "libredwg": ["libredwg", "oda"]}.get(prefer, ["oda", "libredwg"])
    errors: List[str] = []
    for name in order:
        finder = find_oda if name == "oda" else find_libredwg
        if not finder():
            continue
        tmpdir = tempfile.mkdtemp(prefix="dwg2dxf_")
        try:
            _say(on_progress, f"[dwg] converting with {name} …")
            if name == "oda":
                return convert_with_oda(dwg_path, tmpdir, dxf_version=dxf_version)
            return convert_with_libredwg(dwg_path, tmpdir)
        except Exception as ex:
            shutil.rmtree(tmpdir, ignore_errors=True)
            errors.append(f"{name}: {ex}")
            _say(on_progress, f"[dwg] {name} failed: {ex}")
    raise RuntimeError("No DWG→DXF converter succeeded.\n" + ("\n".join(errors) or "(none installed)"))


def guidance_text(version_name: str = "") -> str:
    v = f"（此檔為 DWG {version_name}）" if version_name and version_name != "unknown" else ""
    return (
        f"找不到可讀取此 DWG 的工具{v}。可擇一處理：\n\n"
        "1. 下載 LibreDWG（免費、開源，約 12 MB，免安裝；本工具可代為下載）。\n"
        "2. 安裝 ODA File Converter（品質最佳），裝好後按「重新偵測」或手動指定 ODAFileConverter.exe。\n"
        "   注意：ODA 官方說明非會員僅限非商業用途，公司內使用請先確認授權。\n"
        "3. 請出圖者另存為 DXF；或在 QGIS 使用 專案 → 匯入/匯出 → 「Import Layers from DWG/DXF」。\n\n"
        "GDAL 內建讀取器只支援 DWG R2000（AC1015）。"
    )


def open_dwg(dwg_path: str, prefer: str = "auto", dxf_version: str = "ACAD2013",
             on_progress: ProgressCB = None) -> Tuple[str, str]:
    """Resolve a DWG into something precise_convert() can read.

    Returns (kind, path):
      ("dxf", temp_dxf)   -> caller must later shutil.rmtree(os.path.dirname(temp_dxf))
      ("dwg", dwg_path)   -> read directly with the GDAL CAD driver (R2000 only, limited)
    Raises DwgUnavailableError (with .guidance) when nothing can read it.
    """
    if not os.path.isfile(dwg_path):
        raise FileNotFoundError(f"DWG not found: {dwg_path}")
    code, vname = dwg_version(dwg_path)
    _say(on_progress, f"[dwg] {os.path.basename(dwg_path)}: DWG {vname} ({code or '?'})")

    errors: List[str] = []
    if prefer in ("auto", "oda", "libredwg") and (find_oda() or find_libredwg()):
        try:
            return "dxf", dwg_to_temp_dxf_auto(dwg_path, prefer, dxf_version, on_progress)
        except Exception as ex:
            errors.append(str(ex))

    if gdal_cad_available():
        if code in GDAL_CAD_VERSIONS:
            _say(on_progress, "[dwg] using GDAL CAD driver (DWG R2000; blocks/hatch/text may be incomplete)")
            return "dwg", dwg_path
        errors.append(f"GDAL CAD driver: DWG {vname} not supported (R2000 only)")

    raise DwgUnavailableError(
        "Cannot read DWG: " + ("; ".join(errors) if errors else "no DWG reader available"),
        guidance=guidance_text(vname), version=vname)


# ---------------------------------------------------------------------
# LibreDWG on-demand download (Windows binaries from the official GitHub releases)
# ---------------------------------------------------------------------

def libredwg_download_supported() -> bool:
    return os.name == "nt"


def _http_get(url: str, timeout: int = 60):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "CAD_to_GIS"})
    return urllib.request.urlopen(req, timeout=timeout)   # honours system/IE proxy settings


def _latest_libredwg_version() -> str:
    """Resolve the latest tag via the /releases/latest redirect (no API rate limit)."""
    try:
        with _http_get(LIBREDWG_RELEASES_URL + "/latest", timeout=30) as r:
            m = re.search(r"/releases/tag/([^/?#]+)", r.geturl())
            if m:
                return m.group(1)
    except Exception:
        pass
    return LIBREDWG_FALLBACK_VERSION


def download_libredwg(on_progress: ProgressCB = None,
                      on_percent: Optional[Callable[[int], None]] = None,
                      version: Optional[str] = None) -> str:
    """Download + unzip LibreDWG win64 into the user config folder; returns dwg2dxf.exe path.

    Only call this after the user agreed. LibreDWG is GPL-3.0; the zip comes from
    github.com/LibreDWG/libredwg/releases. Its SHA-256 is logged for traceability.
    """
    if not libredwg_download_supported():
        raise RuntimeError("Automatic download is only available on Windows. "
                           "On Linux/macOS install LibreDWG with your package manager "
                           "(it provides 'dwg2dxf').")
    ver = version or _latest_libredwg_version()
    asset = f"libredwg-{ver}-win64.zip"
    url = f"{LIBREDWG_RELEASES_URL}/download/{ver}/{asset}"
    target = _managed_libredwg_dir()
    tmp_zip = os.path.join(tempfile.gettempdir(), asset)
    _say(on_progress, f"[libredwg] downloading {url}")

    h = hashlib.sha256()
    with _http_get(url, timeout=60) as r, open(tmp_zip, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, last = 0, -1
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if total and on_percent:
                pct = int(got * 100 / total)
                if pct != last:
                    last = pct
                    try:
                        on_percent(pct)
                    except Exception:
                        pass
    _say(on_progress, f"[libredwg] {asset}: {got / 1e6:.1f} MB, SHA-256 {h.hexdigest()}")

    try:
        if not zipfile.is_zipfile(tmp_zip):
            raise RuntimeError("Downloaded file is not a zip archive (proxy / firewall page?).")
        dest = os.path.join(target, ver)
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(tmp_zip) as z:
            for m in z.namelist():                      # refuse path traversal
                p = os.path.abspath(os.path.join(dest, m))
                if not p.startswith(os.path.abspath(dest) + os.sep) and p != os.path.abspath(dest):
                    raise RuntimeError(f"Unsafe path in zip: {m}")
            z.extractall(dest)
    finally:
        try:
            os.remove(tmp_zip)
        except Exception:
            pass

    hits = glob.glob(os.path.join(dest, "**", "dwg2dxf.exe"), recursive=True)
    if not hits:
        raise RuntimeError("dwg2dxf.exe not found in the downloaded archive.")
    set_libredwg_path(hits[0])
    _say(on_progress, f"[libredwg] ready: {hits[0]}")
    return hits[0]


def open_url(url: str) -> None:
    """Open a web page in the default browser (used for the ODA download page)."""
    import webbrowser
    webbrowser.open(url)
