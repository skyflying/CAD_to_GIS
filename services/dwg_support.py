from __future__ import annotations
import os
import tempfile
import shutil
import subprocess
from typing import Optional, List

# ---------- helpers ----------
def _is_file(p: Optional[str]) -> bool:
    return bool(p) and os.path.isfile(p)  # type: ignore[arg-type]

def _which(cmd: str) -> Optional[str]:
    # cross-platform 'which'
    for path in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(path.strip('"'), cmd)
        if os.path.isfile(p):
            return p
        if os.name == "nt":
            p_exe = p if p.lower().endswith(".exe") else p + ".exe"
            if os.path.isfile(p_exe):
                return p_exe
    return None

# ---------- ODA detection ----------
def find_oda() -> Optional[str]:
    # 1) explicit env
    oda = os.environ.get("ODA_CONVERTER")
    if _is_file(oda):
        return os.path.abspath(oda)

    # 2) PATH
    for name in ("ODAFileConverter.exe", "ODAFileConverter_x64.exe", "ODAFileConverter"):
        p = _which(name)
        if _is_file(p):
            return os.path.abspath(p)

    # 3) common Windows install roots (best-effort)
    if os.name == "nt":
        roots = [
            r"C:\Program Files\ODA",
            r"C:\Program Files (x86)\ODA",
            r"C:\Program Files\Open Design Alliance",
            r"C:\Program Files (x86)\Open Design Alliance",
        ]
        for root in roots:
            if os.path.isdir(root):
                for base, _dirs, files in os.walk(root):
                    for exe in ("ODAFileConverter.exe", "ODAFileConverter_x64.exe"):
                        if exe in files:
                            return os.path.abspath(os.path.join(base, exe))
    return None

# ---------- LibreDWG detection ----------
def find_libredwg() -> Optional[str]:
    for name in ("dwg2dxf", "dwg2dxf.exe"):
        p = _which(name)
        if _is_file(p):
            return os.path.abspath(p)
    return None

# ---------- Convert via ODA ----------
def convert_with_oda(dwg_path: str, out_dir: str, dxf_version: str = "ACAD2013") -> str:
    exe = find_oda()
    if not exe:
        raise RuntimeError(
            "ODA File Converter not found. Install it and set ODA_CONVERTER or add it to PATH."
        )
    in_folder = os.path.dirname(os.path.abspath(dwg_path)) or "."
    out_folder = out_dir
    in_filter = "*"
    out_version = dxf_version
    out_type = "DXF"
    recurse = "0"

    cmd = [
        exe, in_folder, out_folder, in_filter, out_version, out_type, recurse,
        os.path.abspath(dwg_path),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ODA converter failed (code {res.returncode}). Output:\n{res.stdout}")

    # Expected file
    base = os.path.splitext(os.path.basename(dwg_path))[0] + ".dxf"
    out_path = os.path.join(out_folder, base)
    if os.path.isfile(out_path):
        return out_path

    # Some ODA builds write into subfolders; search
    found: List[str] = []
    for b, _d, f in os.walk(out_folder):
        for fn in f:
            if fn.lower().endswith(".dxf") and os.path.splitext(fn)[0].lower() == os.path.splitext(base)[0].lower():
                found.append(os.path.join(b, fn))
    if not found:
        for b, _d, f in os.walk(out_folder):
            for fn in f:
                if fn.lower().endswith(".dxf"):
                    found.append(os.path.join(b, fn))
    if not found:
        raise RuntimeError("ODA conversion finished but no DXF was produced.")

    if found[0] != out_path:
        try:
            shutil.move(found[0], out_path)
        except Exception:
            out_path = found[0]
    return out_path

# ---------- Convert via LibreDWG ----------
def convert_with_libredwg(dwg_path: str, out_dir: str) -> str:
    exe = find_libredwg()
    if not exe:
        raise RuntimeError("LibreDWG 'dwg2dxf' not found on PATH.")
    out_path = os.path.join(out_dir, os.path.splitext(os.path.basename(dwg_path))[0] + ".dxf")
    cmd = [exe, "-o", out_path, os.path.abspath(dwg_path)]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if res.returncode != 0 or not os.path.isfile(out_path):
        raise RuntimeError(f"dwg2dxf failed (code {res.returncode}). Output:\n{res.stdout}")
    return out_path

# ---------- Public API ----------
def detect_dwg_converter() -> str:
    """
    Return a short label of available converter:
      - 'oda' if ODA is found
      - 'libredwg' if dwg2dxf is found
      - '' if none
    """
    if find_oda():
        return "oda"
    if find_libredwg():
        return "libredwg"
    return ""

def dwg_to_temp_dxf_auto(dwg_path: str, prefer: str = "auto", dxf_version: str = "ACAD2013") -> str:
    """
    Convert DWG to a temporary DXF using whichever converter is available.
    prefer: 'auto' | 'oda' | 'libredwg'
    Returns the temp DXF path. Caller may delete it later.
    """
    if not os.path.isfile(dwg_path):
        raise FileNotFoundError(f"DWG not found: {dwg_path}")

    tmpdir = tempfile.mkdtemp(prefix="dwg2dxf_")
    try:
        if prefer == "oda":
            return convert_with_oda(dwg_path, tmpdir, dxf_version=dxf_version)
        if prefer == "libredwg":
            return convert_with_libredwg(dwg_path, tmpdir)

        # auto: try ODA then LibreDWG
        try:
            return convert_with_oda(dwg_path, tmpdir, dxf_version=dxf_version)
        except Exception:
            return convert_with_libredwg(dwg_path, tmpdir)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
