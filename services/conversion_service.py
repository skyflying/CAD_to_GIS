# -*- coding: utf-8 -*-


from __future__ import annotations
from typing import Tuple, Dict, List, Optional, Callable
import re
import pandas as pd

ProgressCB = Optional[Callable[[str], None]]


def _sanitize_filename(name: str) -> str:
    """Sanitize a name for filesystem and OGR layer names (Windows-safe)."""
    import re as _re
    s = "layer" if name is None else str(name)
    s = _re.sub(r"[^A-Za-z0-9 _.\-]+", "_", s).strip(" .")
    if not s:
        s = "layer"
    reserved = {"CON","PRN","AUX","NUL",
                "COM1","COM2","COM3","COM4","COM5","COM6","COM7","COM8","COM9",
                "LPT1","LPT2","LPT3","LPT4","LPT5","LPT6","LPT7","LPT8","LPT9"}
    if s.upper() in reserved:
        s = f"_{s}_"
    return s[:100]



def _sanitize_fields_for_driver(df, driver: str):

    try:
        import geopandas as gpd  
    except Exception:
        pass

    drv = (driver or "").upper()
    geom_col_name = getattr(df, "geometry", None).name if hasattr(df, "geometry") and df.geometry is not None else "geometry"

    rename_map = {}
    if drv == "GPKG":
        for col in list(df.columns):
            if col == geom_col_name:
                continue  # this is the active geometry column
            if col.lower() == "geom":  # conflict with GPKG geometry column name used by Fiona
                new_name = "geom_type"
                i = 1
                # ensure uniqueness
                while new_name in df.columns or new_name == geom_col_name:
                    new_name = f"geom_type_{i}"
                    i += 1
                rename_map[col] = new_name

    if rename_map:
        df = df.rename(columns=rename_map)
    return df




def _coerce_paths(src_path, dxf_paths_kw=None) -> List[str]:
    """Normalize incoming path(s): string, list, or stringified list → list[str]."""
    import ast
    out: List[str] = []

    def _eat(x):
        if not x:
            return
        if isinstance(x, (list, tuple)):
            for p in x:
                if p:
                    out.append(str(p))
        elif isinstance(x, str):
            s = x.strip()
            if s.startswith("[") and s.endswith("]"):
                try:
                    arr = ast.literal_eval(s)
                    if isinstance(arr, (list, tuple)):
                        for p in arr:
                            if p:
                                out.append(str(p))
                        return
                except Exception:
                    pass
                s2 = s.strip("[]").strip().strip("'\"")
                if s2:
                    out.append(s2)
            else:
                out.append(s)

    _eat(dxf_paths_kw)
    _eat(src_path)

    seen = set()
    norm = []
    for p in out:
        if p not in seen:
            seen.add(p); norm.append(p)
    return norm



def _coords_to_geom(coords: List[tuple]):
    """Convert coordinate sequence into (geom_type, shapely geom)."""
    from shapely.geometry import Point, LineString, Polygon
    xy = [(x, y) for x, y, _ in coords]
    if not xy:
        return None, None
    if len(xy) == 1:
        return "POINT", Point(xy[0])
    if xy[0] == xy[-1] and len(xy) >= 4:
        return "POLYGON", Polygon(xy)
    return "LINE", LineString(xy)


def _extract_lineal(geom):
    """Extract a pure (Multi)LineString from any geometry (collection-safe)."""
    from shapely.geometry import LineString, MultiLineString, GeometryCollection
    from shapely.ops import unary_union, linemerge
    if geom is None:
        return None
    if isinstance(geom, (LineString, MultiLineString)):
        return geom if not geom.is_empty else None
    if isinstance(geom, GeometryCollection):
        lines = []
        for g in geom.geoms:
            if isinstance(g, (LineString, MultiLineString)) and not g.is_empty:
                lines.append(g)
        if not lines:
            return None
        try:
            m = linemerge(unary_union(lines))
            return m if m and not m.is_empty else None
        except Exception:
            flat = []
            for g in lines:
                if isinstance(g, LineString) and not g.is_empty:
                    flat.append(g)
                elif isinstance(g, MultiLineString):
                    flat.extend([ls for ls in g.geoms if not ls.is_empty])
            from shapely.geometry import MultiLineString as MLS
            return MLS(flat) if flat else None
    return None


def _count_line_parts(g):
    """Return number of line parts (1 for LineString, N for MultiLineString, 0 else)."""
    from shapely.geometry import LineString, MultiLineString
    if g is None or getattr(g, "is_empty", False):
        return 0
    if isinstance(g, LineString):
        return 1
    if isinstance(g, MultiLineString):
        return len(getattr(g, "geoms", []))
    return 0


def _as_single_multiline(parts):
    """Pack list[LineString] → one MultiLineString safely."""
    from shapely.geometry import MultiLineString
    return MultiLineString([ls for ls in parts if ls and not getattr(ls, "is_empty", False)])


def _grid_snap_lines(lines, tol: float):
    """Snap endpoints to a tolerance grid to improve merging; mid-points kept as-is."""
    from shapely.geometry import LineString
    if tol <= 0 or not lines:
        return lines

    def q(x): return round(x / tol) * tol

    out = []
    for ls in lines:
        try:
            coords = list(ls.coords)
            if len(coords) < 2:
                continue
            head = (q(coords[0][0]), q(coords[0][1]))
            tail = (q(coords[-1][0]), q(coords[-1][1]))
            mid = coords[1:-1]
            new = [head] + mid + [tail]
            clean = [new[0]]
            for p in new[1:]:
                if p != clean[-1]:
                    clean.append(p)
            if len(clean) >= 2:
                ls2 = LineString(clean)
                if ls2.length > 0:
                    out.append(ls2)
        except Exception:
            continue
    return out


def _merge_lines_robust(lines, tol: float, say=lambda m: None):
    """
    Robust line merging:
      tol <= 0: unary_union + linemerge (exact endpoint merges)
      tol  > 0: endpoint grid-snap → unary_union + linemerge
    """
    from shapely.ops import unary_union, linemerge
    if not lines:
        return None
    try:
        L1 = _grid_snap_lines(lines, tol) if tol > 0 else lines
        if not L1:
            return None
        u1 = unary_union(L1)
        m1 = linemerge(u1)
        if m1 and not getattr(m1, "is_empty", False):
            return m1
    except Exception as ex:
        say(f"[merge] robust exception: {ex}")
    return None


def _merge_lines_graph(lines, tol: float):
    """Greedy graph stitcher (endpoints bucketed by tolerance)."""
    from shapely.geometry import LineString, MultiLineString
    if not lines:
        return None

    def q(x): return round(x / tol) * tol if tol > 0 else x

    endpoints = []
    for ls in lines:
        try:
            coords = list(ls.coords)
            if len(coords) < 2:
                continue
            a = (q(coords[0][0]), q(coords[0][1]))
            b = (q(coords[-1][0]), q(coords[-1][1]))
            endpoints.append((a, b, coords))
        except Exception:
            continue

    if not endpoints:
        return None

    from collections import defaultdict as _dd
    node_deg = _dd(int)
    adj = _dd(list)
    edges = []
    for i, (a, b, coords) in enumerate(endpoints):
        edges.append((a, b, coords))
        adj[a].append(i); adj[b].append(i)
        node_deg[a] += 1; node_deg[b] += 1
    used_edge = [False]*len(edges)

    def build_path(start_node):
        path = []
        cur = start_node
        while True:
            nxt = None
            for ei in adj[cur]:
                if not used_edge[ei]:
                    nxt = ei; break
            if nxt is None:
                break
            used_edge[nxt] = True
            a, b, coords = edges[nxt]
            if a == cur:
                seg = coords; other = b
            else:
                seg = list(reversed(coords)); other = a
            if not path:
                path.extend(seg)
            else:
                path.extend(seg[1:] if path[-1] == seg[0] else seg)
            if node_deg[other] != 2:
                cur = other
                break
            cur = other
        return path

    merged = []
    for node, deg in node_deg.items():
        if deg != 2:
            while any(not used_edge[e] for e in adj[node]):
                coords = build_path(node)
                if coords and len(coords) >= 2:
                    try: merged.append(LineString(coords))
                    except Exception: pass

    for ei, used in enumerate(used_edge):
        if not used:
            a, b, coords0 = edges[ei]
            used_edge[ei] = True
            cur_coords = list(coords0); cur_node = b
            while True:
                nxt = None
                for ej in adj[cur_node]:
                    if not used_edge[ej]:
                        nxt = ej; break
                if nxt is None:
                    break
                used_edge[nxt] = True
                a2, b2, coords2 = edges[nxt]
                if a2 == cur_node:
                    seg = coords2; cur_node = b2
                else:
                    seg = list(reversed(coords2)); cur_node = a2
                cur_coords.extend(seg[1:] if cur_coords[-1] == seg[0] else seg)
                if node_deg[cur_node] != 2:
                    break
            if len(cur_coords) >= 2:
                try: merged.append(LineString(cur_coords))
                except Exception: pass

    if not merged:
        return None
    return merged[0] if len(merged) == 1 else MultiLineString([ls for ls in merged if ls.length > 0])


# ---------------------------------------------------------------------
# ezdxf path (kept from v1.1 behavior)
# ---------------------------------------------------------------------

def _flatten_with_path(e, dist: float) -> List[tuple]:
    from ezdxf import path as ezpath
    p = ezpath.make_path(e)
    return [(v.x, v.y, 0.0) for v in p.flattening(distance=dist)]


def _fallback_polyline(e, include_3d: bool) -> List[tuple]:
    pts=[]
    if hasattr(e,"get_points"):
        for v in e.get_points(): pts.append((v[0], v[1], 0.0))
    else:
        for v in e:
            loc=v.dxf.location; pts.append((loc.x, loc.y, loc.z if include_3d else 0.0))
    return pts


def _fallback_circle(e, dist: float) -> List[tuple]:
    import math
    c=e.dxf.center; r=float(e.dxf.radius)
    segs=max(24, int(6.28318530718/max(dist,0.1)))
    return [(c.x+r*math.cos(2*math.pi*i/segs), c.y+r*math.sin(2*math.pi*i/segs), 0.0)
            for i in range(segs+1)]


def _fallback_arc(e, dist: float) -> List[tuple]:
    import math
    c=e.dxf.center; r=float(e.dxf.radius)
    a1=math.radians(float(e.dxf.start_angle)); a2=math.radians(float(e.dxf.end_angle))
    if a2<a1: a1,a2=a2,a1
    steps=max(16, int((a2-a1)/max(dist,0.05)))
    return [(c.x+r*math.cos(a1+(a2-a1)*i/steps), c.y+r*math.sin(a1+(a2-a1)*i/steps), 0.0)
            for i in range(steps+1)]


def _flatten_hatch_rings(e) -> List[List[tuple]]:
    rings=[]
    try:
        for p in e.paths.polygons:
            ring=[(pt[0],pt[1],0.0) for pt in p]
            if ring and ring[0]!=ring[-1]: ring.append(ring[0])
            if ring: rings.append(ring)
    except Exception: pass
    try:
        if not rings:
            for path in e.paths:
                ring=[]
                for edge in path.edges:
                    if hasattr(edge,"start"):
                        s=edge.start; ring.append((s[0],s[1],0.0))
                if ring and ring[0]!=ring[-1]: ring.append(ring[0])
                if ring: rings.append(ring)
    except Exception: pass
    return rings


def _precise_rows_from_entity(e, layer, include_3d, dist) -> List[dict]:
    from shapely.geometry import Point as _Pt
    rows=[]
    t=e.dxftype()
    if t in {"LINE","LWPOLYLINE","POLYLINE","CIRCLE","ARC","ELLIPSE","SPLINE"}:
        try: pts=_flatten_with_path(e, dist)
        except Exception:
            if t=="LINE":
                s,ed=e.dxf.start,e.dxf.end
                pts=[(s.x,s.y,s.z if include_3d else 0.0),(ed.x,ed.y,ed.z if include_3d else 0.0)]
            elif t in {"LWPOLYLINE","POLYLINE"}: pts=_fallback_polyline(e, include_3d)
            elif t=="CIRCLE": pts=_fallback_circle(e, dist)
            elif t=="ARC": pts=_fallback_arc(e, dist)
            else: pts=[]
        if pts:
            gtype,geom=_coords_to_geom(pts)
            if gtype and geom: rows.append({"layer":layer,"geom":gtype,"geometry":geom})
    elif t=="POINT":
        p=e.dxf.location; rows.append({"layer":layer,"geom":"POINT","geometry":_Pt(p.x,p.y)})
    elif t=="HATCH":
        for ring in _flatten_hatch_rings(e):
            gtype,geom=_coords_to_geom(ring)
            if gtype and geom: rows.append({"layer":layer,"geom":gtype,"geometry":geom})
    elif t in {"3DFACE","THREE_D_FACE","SOLID"}:
        try:
            v0,v1,v2,v3=e.dxf.vtx0,e.dxf.vtx1,e.dxf.vtx2,e.dxf.vtx3
            ring=[(v0.x,v0.y,0.0),(v1.x,v1.y,0.0),(v2.x,v2.y,0.0),(v3.x,v3.y,0.0),(v0.x,v0.y,0.0)]
            gtype,geom=_coords_to_geom(ring)
            if gtype and geom: rows.append({"layer":layer,"geom":gtype,"geometry":geom})
        except Exception: pass
    return rows


def _extract_block_meta(e) -> dict:
    """Extract block metadata from INSERT (name, pose, attributes)."""
    meta={}
    try:
        bname=(getattr(e.dxf,"name",None) or getattr(e,"name",None) or ""); meta["block_name"]=str(bname)
    except Exception: meta["block_name"]=""
    try:
        ip=e.dxf.insert
        meta["BLK_X"]=float(getattr(ip,"x",0.0)); meta["BLK_Y"]=float(getattr(ip,"y",0.0)); meta["BLK_Z"]=float(getattr(ip,"z",0.0))
    except Exception: pass
    for k_src,k_out in [("rotation","BLK_ROT"),("xscale","BLK_SX"),("yscale","BLK_SY"),("zscale","BLK_SZ")]:
        try:
            v=getattr(e.dxf,k_src,None)
            if v is not None: meta[k_out]=float(v)
        except Exception: pass
    try:
        for a in getattr(e,"attribs",[]) or []:
            try:
                tag=str(a.dxf.tag).strip(); txt=a.dxf.text
                if not tag: continue
                key="ATT_"+re.sub(r"[^0-9A-Za-z_]+","_", tag.upper())
                meta[key]="" if txt is None else str(txt)
            except Exception: continue
    except Exception: pass
    return meta


# ---------------------------------------------------------------------
# ezdxf conversion (v1.1 semantics)
# ---------------------------------------------------------------------

def _precise_convert_ezdxf(
    dxf_paths: List[str],
    *,
    source_epsg: int = 3826,
    target_epsg: Optional[int] = None,
    include_3d: bool = False,
    flat_dist_precise: float = 0.2,
    target_layers: Optional[List[str]] = None,
    bbox_wgs84: Optional[Tuple[float, float, float, float]] = None,
    on_progress: ProgressCB = None,
    block_mode: str = "keep-merge",
    line_merge_tol: float = 0.2,
) -> Dict[Tuple[str, str], "gpd.GeoDataFrame"]:
    import time, ezdxf, geopandas as gpd

    def say(m):
        if on_progress:
            try: on_progress(m)
            except: pass

    sel_layers = set(target_layers) if target_layers else None
    say(f"[convert] srcEPSG={source_epsg} tgtEPSG={target_epsg} bbox={bbox_wgs84} include_3d={include_3d} block_mode={block_mode}")

    rows: List[dict] = []
    for path in dxf_paths:
        try:
            say(f"[convert] read: {path}")
            doc = ezdxf.readfile(path)
            msp = doc.modelspace()
        except Exception as ex:
            say(f"[error] read failed: {ex}")
            continue

        count = 0
        ins_count = 0
        for e in msp:
            count += 1
            if count % 1000 == 0:
                say(f"[convert] processing… {count} entities")

            try:
                t = e.dxftype()
                layer = getattr(e.dxf, "layer", "0") or "0"
                if sel_layers and layer not in sel_layers:
                    continue

                if t == "INSERT":
                    ins_count += 1
                    if ins_count % 50 == 0:
                        say(f"[convert] …INSERT expanded: {ins_count}")

                    meta = _extract_block_meta(e)
                    mode = (block_mode or "explode").lower().strip()

                    if mode in ("keep-merge", "keep-merge-per"):
                        from shapely.geometry import LineString
                        polys = []
                        lines = []
                        segs_seen = 0

                        def _push_line_from_pts(pts):
                            nonlocal segs_seen
                            if not pts:
                                return
                            xy = [(x, y) for x, y, _ in pts]
                            if len(xy) >= 2:
                                if xy[0] == xy[-1] and len(xy) >= 3:
                                    xy = xy[:-1]
                                if len(xy) >= 2:
                                    try:
                                        lines.append(LineString(xy)); segs_seen += 1
                                    except Exception:
                                        pass
                            if segs_seen and segs_seen % 5000 == 0:
                                say(f"[keep] block={meta.get('block_name','')} collected {segs_seen} segments…")

                        try:
                            for se in e.virtual_entities():
                                sl = getattr(se.dxf, "layer", "0") or "0"
                                if sel_layers and sl not in sel_layers:
                                    continue
                                st = se.dxftype()
                                if st in {"LINE","LWPOLYLINE","POLYLINE","ARC","CIRCLE","ELLIPSE","SPLINE"}:
                                    try:
                                        pts = _flatten_with_path(se, flat_dist_precise)
                                    except Exception:
                                        if st == "LINE":
                                            s, ed = se.dxf.start, se.dxf.end
                                            pts = [(s.x, s.y, 0.0), (ed.x, ed.y, 0.0)]
                                        elif st in {"LWPOLYLINE","POLYLINE"}:
                                            pts = _fallback_polyline(se, False)
                                        elif st == "CIRCLE":
                                            pts = _fallback_circle(se, flat_dist_precise)
                                        elif st == "ARC":
                                            pts = _fallback_arc(se, flat_dist_precise)
                                        else:
                                            pts = []
                                    _push_line_from_pts(pts)
                        except Exception as ex:
                            say(f"[warn] virtual_entities failed: {ex}")

                        merged = _merge_lines_robust(lines, float(line_merge_tol or 0.0), say) \
                                 or _merge_lines_graph(lines, float(line_merge_tol or 0.0)) \
                                 or _as_single_multiline(lines)

                        row = {"layer": layer, "geom": "LINE", "geometry": merged}
                        row.update(meta); rows.append(row)
                        continue

                    # explode mode
                    expanded = False
                    try:
                        for se in e.virtual_entities():
                            sl = getattr(se.dxf, "layer", "0") or "0"
                            if sel_layers and sl not in sel_layers:
                                continue
                            rws = _precise_rows_from_entity(se, sl, include_3d, flat_dist_precise)
                            for r in rws: r.update(meta)
                            rows += rws; expanded = True
                    except Exception as ex:
                        say(f"[warn] INSERT explode failed: {ex}")
                    if not expanded:
                        ip = e.dxf.insert
                        from shapely.geometry import Point as _Pt
                        row = {"layer": layer, "geom": "POINT", "geometry": _Pt(ip.x, ip.y)}
                        row.update(meta); rows.append(row)
                    continue

                # non-INSERT
                rows += _precise_rows_from_entity(e, layer, include_3d, flat_dist_precise)
            except Exception as ex:
                say(f"[warn] entity failed: {ex}")
                continue

        say(f"[convert] done file: {path}, total rows {len(rows)}")

    if not rows:
        say("[convert] no rows"); return {}

    import geopandas as gpd
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=f"EPSG:{source_epsg or 4326}")

    out: Dict[Tuple[str, str], "gpd.GeoDataFrame"] = {}
    for (layer, geom), sub in gdf.groupby(["layer", "geom"]):
        say(f"[group] {layer} / {geom}: {len(sub)}")
        out[(str(layer), str(geom))] = sub.reset_index(drop=True)
    say(f"[convert] grouped buckets: {len(out)}")
    return out


# ---------------------------------------------------------------------
# OGR helpers (fallback)
# ---------------------------------------------------------------------

def _collapse_gc_preserve_multis(sgeom):
    """Collapse a geometry collection into LINE / POLYGON / POINT without exploding to tiny parts."""
    from shapely.geometry import (
        Point, MultiPoint, LineString, MultiLineString, Polygon, MultiPolygon, GeometryCollection
    )
    from shapely.ops import unary_union, linemerge

    pts, lines, polys = [], [], []

    def visit(g):
        if g is None or g.is_empty:
            return
        if isinstance(g, Point):
            pts.append(g)
        elif isinstance(g, MultiPoint):
            pts.extend(list(g.geoms))
        elif isinstance(g, LineString):
            if g.length > 0:
                lines.append(g)
        elif isinstance(g, MultiLineString):
            for ls in g.geoms:
                if ls.length > 0:
                    lines.append(ls)
        elif isinstance(g, Polygon):
            if g.area >= 0.0:
                polys.append(g)
        elif isinstance(g, MultiPolygon):
            polys.extend(list(g.geoms))
        elif isinstance(g, GeometryCollection):
            for c in g.geoms:
                visit(c)

    visit(sgeom)

    out = {}
    if lines:
        try:
            merged = linemerge(unary_union(lines))
            if merged and not merged.is_empty:
                out["LINE"] = merged
            else:
                from shapely.geometry import MultiLineString as MLS
                out["LINE"] = MLS([list(ls.coords) for ls in lines if not ls.is_empty])
        except Exception:
            from shapely.geometry import MultiLineString as MLS
            out["LINE"] = MLS([list(ls.coords) for ls in lines if not ls.is_empty])

    if polys:
        try:
            from shapely.ops import unary_union
            u = unary_union(polys)
            if u and not u.is_empty:
                out["POLYGON"] = u
        except Exception:
            from shapely.geometry import MultiPolygon as MP
            out["POLYGON"] = MP([p for p in polys if not p.is_empty])

    if pts:
        if len(pts) == 1:
            out["POINT"] = pts[0]
        else:
            from shapely.geometry import MultiPoint as MPt
            out["POINT"] = MPt(pts)

    return out


def _find_columns_case_insensitive(cols, candidates: List[str]) -> List[str]:
    low2real = {c.lower(): c for c in cols}
    found = []
    for cand in candidates:
        r = low2real.get(cand.lower())
        if r and r not in found:
            found.append(r)
    return found


# ---------- INSERT pass (DXF_INLINE_BLOCKS=NO) ----------

def _ogr_collect_inserts(dxf_paths, target_layers, on_progress=None):
    """
    Open DXF with DXF_INLINE_BLOCKS=NO and collect INSERT instances.
    Returns a list of dicts:
      {"block_name", "x", "y", "rot", "sx", "sy", "sz", "layer"}
    """
    def say(m):
        if on_progress:
            try: on_progress(m)
            except: pass

    inserts = []
    try:
        from osgeo import ogr, gdal
    except Exception:
        return inserts

    sel = set(target_layers) if target_layers else None

    # Temporarily disable inlining to access INSERT features
    try:
        gdal.SetConfigOption("DXF_INLINE_BLOCKS", "NO")
        gdal.SetConfigOption("DXF_BLOCK_ATTRIBUTES", "YES")
    except Exception:
        pass

    for path in dxf_paths:
        ds = ogr.Open(path, 0)
        if ds is None:
            continue
        lyr = ds.GetLayerByName("entities") or (ds.GetLayer(0) if ds.GetLayerCount() else None)
        if lyr is None:
            ds = None; continue
        defn = lyr.GetLayerDefn()
        flds = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]

        lyr.ResetReading()
        for f in lyr:
            try:
                # Try to read an INSERT-like record by checking block name fields and point geometry
                bname = None
                for cand in ("BlockName", "BLOCKNAME", "Block", "BLOCK", "DXF_BLOCK", "DXF_BLOCK_NAME"):
                    if cand in flds:
                        bname = f.GetField(cand)
                        if bname:
                            break
                if not bname:
                    continue

                g = f.GetGeometryRef()
                if g is None:
                    continue
                gtyp = g.GetGeometryType()
                # Accept Point(1) or MultiPoint(4)
                if gtyp not in (1, 4):
                    continue

                # Use first point coordinate as insert position
                if gtyp == 1:
                    x = g.GetX(); y = g.GetY()
                else:
                    # MultiPoint: take first point
                    subg = g.GetGeometryRef(0)
                    if subg is None:
                        continue
                    x = subg.GetX(); y = subg.GetY()

                rot = None
                for cand in ("Rotation","Angle","BlockRotation","BlockAngle"):
                    if cand in flds:
                        rot = f.GetField(cand); break
                sx = sy = sz = None
                for cand in ("ScaleX","BlockScaleX","XScale"):
                    if cand in flds: sx = f.GetField(cand); break
                for cand in ("ScaleY","BlockScaleY","YScale"):
                    if cand in flds: sy = f.GetField(cand); break
                for cand in ("ScaleZ","BlockScaleZ","ZScale"):
                    if cand in flds: sz = f.GetField(cand); break
                layer = f.GetField("Layer") if "Layer" in flds else "0"
                if sel and layer not in sel:
                    continue

                inserts.append({
                    "block_name": str(bname),
                    "x": float(x), "y": float(y),
                    "rot": float(rot) if rot is not None else None,
                    "sx": float(sx) if sx is not None else None,
                    "sy": float(sy) if sy is not None else None,
                    "sz": float(sz) if sz is not None else None,
                    "layer": str(layer),
                })
            except Exception:
                continue
        ds = None

    # Restore default (inline again) for later steps
    try:
        gdal.SetConfigOption("DXF_INLINE_BLOCKS", "YES")
    except Exception:
        pass
    return inserts


def _assign_block_instance_ids(lines_gdf, inserts, tol_eff, on_progress=None):
    """
    Tag each line with a '_blk_id' (block instance id) using nearest INSERT point
    of the same BlockName (preferred). Falls back conservatively.
    """
    def say(m):
        if on_progress:
            try: on_progress(m)
            except: pass

    import numpy as np
    import geopandas as gpd
    from shapely.geometry import Point
    try:
        from shapely.strtree import STRtree
    except Exception:
        # If STRtree is unavailable, return without assignment
        return lines_gdf.assign(_blk_id=pd.Series([None]*len(lines_gdf)))

    if not len(lines_gdf) or not inserts:
        return lines_gdf.assign(_blk_id=pd.Series([None]*len(lines_gdf)))

    ins_df = pd.DataFrame(inserts)
    ins_g = gpd.GeoSeries([Point(xy) for xy in zip(ins_df["x"], ins_df["y"])], crs=lines_gdf.crs)
    ins_df = gpd.GeoDataFrame(ins_df, geometry=ins_g, crs=lines_gdf.crs)

    # Build per-block-name spatial index
    trees = {}
    name_groups = {}
    for name, sub in ins_df.groupby(ins_df["block_name"].astype(str)):
        if len(sub) == 0:
            continue
        trees[name] = STRtree(list(sub.geometry.values))
        name_groups[name] = sub.reset_index(drop=True)

    blk_names_col = None
    for cand in ("block_name","BlockName","BLOCKNAME","BLOCK_NAME","Block","BLOCK","BlockRef","BlockRefName","DXF_BLOCK","DXF_BLOCK_NAME"):
        if cand in lines_gdf.columns:
            blk_names_col = cand; break

    # First pass: nearest distance per block name
    line_cent = lines_gdf.geometry.centroid
    nearest_id = [None]*len(lines_gdf)
    nearest_dist = [float("inf")]*len(lines_gdf)

    for i, (geom, bname) in enumerate(zip(line_cent.values, (lines_gdf[blk_names_col].astype(str) if blk_names_col else pd.Series([""]*len(lines_gdf))))):
        try:
            if bname in trees:
                idxs = trees[bname].nearest(geom, 1)
                if not isinstance(idxs, (list, tuple)):
                    idxs = [idxs]
                j = int(idxs[0])
                ins_row = name_groups[bname].iloc[j]
                d = geom.distance(ins_row.geometry)
                nearest_id[i] = (bname, j)  # (name, local-index in name_groups[name])
                nearest_dist[i] = d
        except Exception:
            continue

    # Learn per-name radius; if insufficient samples, fallback to 10 * tol_eff
    max_by_name = {}
    if blk_names_col:
        arr_df = pd.DataFrame({"bname": lines_gdf[blk_names_col].astype(str), "d": nearest_dist})
        for name, sub in arr_df.groupby("bname"):
            vals = [float(v) for v in sub["d"].values if v != float("inf")]
            if len(vals) >= 8:
                from numpy import percentile
                p80 = float(percentile(vals, 80))
                p50 = float(percentile(vals, 50))
                R = max(3.0*tol_eff, min(max(p80*1.5, p50*2.0), p80*3.0))
            else:
                R = max(10.0*tol_eff, 1.0)
            max_by_name[name] = R
    else:
        max_by_name = {"": max(10.0*tol_eff, 1.0)}

    # Assign blk_id if within learned radius
    blk_ids = [None]*len(lines_gdf)
    for i, nid in enumerate(nearest_id):
        if nid is None:
            continue
        name, j = nid
        R = max_by_name.get(name, max(10.0*tol_eff, 1.0))
        if nearest_dist[i] <= R:
            global_id = int(name_groups[name].index[j])
            blk_ids[i] = f"{name}#{global_id}"

    out = lines_gdf.copy()
    out["_blk_id"] = blk_ids
    return out


# ---------------------------------------------------------------------
# OGR fallback (fast & aligned with ezdxf semantics)
# ---------------------------------------------------------------------

def _convert_ogr_fallback(
    dxf_paths: List[str],
    *,
    source_epsg: int = 3826,
    target_epsg: Optional[int] = None,
    target_layers: Optional[List[str]] = None,
    bbox_wgs84: Optional[Tuple[float, float, float, float]] = None,
    line_merge_tol: float = 0.2,
    flat_dist_precise: float = 0.2,     # used to tune curve discretization
    on_progress: ProgressCB = None,
) -> Dict[Tuple[str, str], "gpd.GeoDataFrame"]:
    """
    Optimized OGR pipeline:
    - SQL-select only requested layers
    - Inline blocks, linearize curves (DXF driver options)
    - Two-pass keep-merge: collect INSERTs (no-inline), then assign lines to nearest INSERT (same name)
    - Merge ONLY within each INSERT instance; conservative fallback to avoid whole-layer collapse
    """
    def say(m):
        if on_progress:
            try: on_progress(m)
            except: pass

    try:
        from osgeo import ogr, gdal
        try:
            ogr.UseExceptions()
        except Exception:
            pass
    except Exception as ex:
        raise RuntimeError(f"OGR fallback requested but GDAL/OGR is not available: {ex}")

    # ---- Driver options for the read (inline path) ----
    try:
        gdal.SetConfigOption("DXF_INLINE_BLOCKS", "YES")
        gdal.SetConfigOption("DXF_BLOCK_ATTRIBUTES", "YES")
        gdal.SetConfigOption("DXF_CLOSED_LINE_AS_POLYGON", "FALSE")  # keep closed polylines as LINE
        # coarser step = fewer vertices = faster; bound between 0.5 and 5.0
        step = max(0.5, min(5.0, float(flat_dist_precise or 0.2)))
        gdal.SetConfigOption("OGR_ARC_STEPSIZE", str(step))
        gdal.SetConfigOption("DXF_ATTRIBUTES_TO_COPY",
                             "BlockName,BlockRotation,BlockAngle,BlockScale,BlockScaleX,BlockScaleY,BlockScaleZ,"
                             "InsertX,InsertY,InsertZ,InsertionX,InsertionY,InsertionZ")
    except Exception:
        pass

    import geopandas as gpd
    from shapely import wkb as _wkb
    from shapely import wkt as _wkt
    from shapely.geometry import LineString, MultiLineString

    sel_layers = set(target_layers) if target_layers else None
    rows: List[dict] = []

    # ---------- INSERT pass (NO inline): collect INSERT instances ----------
    say("[ogr] collecting INSERT instances (no-inline pass)…")
    inserts = _ogr_collect_inserts(dxf_paths, target_layers, on_progress=on_progress)
    say(f"[ogr] INSERT instances found: {len(inserts)}")

    # ---------- Read each DXF via SQL (INLINE) ----------
    for path in dxf_paths:
        say(f"[ogr] read: {path}")
        ds = ogr.Open(path, 0)
        if ds is None:
            say(f"[ogr:error] cannot open: {path}")
            continue

        # Build SQL to restrict to chosen layers
        sql_layer = None
        if sel_layers:
            in_list = ",".join(["'%s'" % l.replace("'", "''") for l in sel_layers])
            sql = f"SELECT * FROM entities WHERE Layer IN ({in_list})"
            try:
                sql_layer = ds.ExecuteSQL(sql)
            except Exception as ex:
                say(f"[ogr:warn] SQL failed ({ex}), fallback to full layer scan")

        lyr = sql_layer or ds.GetLayerByName("entities") or (ds.GetLayer(0) if ds.GetLayerCount() else None)
        if lyr is None:
            say(f"[ogr:warn] no 'entities' layer in: {path}")
            if sql_layer: ds.ReleaseResultSet(sql_layer)
            ds = None; continue

        defn = lyr.GetLayerDefn()
        field_names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]

        total = 0
        kept = 0
        lyr.ResetReading()
        for f in lyr:
            total += 1
            try:
                layer_name = f.GetField("Layer") if "Layer" in field_names else None
                if not layer_name:
                    layer_name = "0"
                if sel_layers and layer_name not in sel_layers:
                    continue

                g = f.GetGeometryRef()
                if g is None:
                    continue

                # Prefer the already-linearized view
                try:
                    lg = g.GetLinearGeometry() or g
                except Exception:
                    lg = g

                sgeom = None
                try:
                    wkb = lg.ExportToWkb()
                    sgeom = _wkb.loads(bytes(wkb))
                except Exception:
                    try:
                        sgeom = _wkt.loads(lg.ExportToWkt())
                    except Exception as ex:
                        say(f"[ogr:warn] geometry export failed: {ex}")
                        continue

                if sgeom is None or sgeom.is_empty:
                    continue

                agg = _collapse_gc_preserve_multis(sgeom)
                if not agg:
                    continue

                attrs = {}
                for i, name in enumerate(field_names):
                    if name.lower() == "layer":
                        continue
                    try:
                        attrs[name] = f.GetField(i)
                    except Exception:
                        pass

                for gcode in ("LINE","POLYGON","POINT"):
                    geom_out = agg.get(gcode)
                    if geom_out is None or geom_out.is_empty:
                        continue
                    row = {"layer": layer_name, "geom": gcode, "geometry": geom_out}
                    row.update(attrs)
                    rows.append(row); kept += 1
            except Exception as ex:
                say(f"[ogr:warn] feature read failed: {ex}")
                continue

        say(f"[ogr] features total={total}, kept={kept}")
        if sql_layer:
            ds.ReleaseResultSet(sql_layer)
        ds = None

    if not rows:
        say("[ogr] no rows")
        return {}

    # Build GeoDataFrame in SOURCE CRS (merge happens here)
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=f"EPSG:{source_epsg or 4326}")

    # Optional bbox (in WGS84) – filter by mask without altering CRS
    if bbox_wgs84 and isinstance(bbox_wgs84, (list, tuple)) and len(bbox_wgs84) == 4:
        from shapely.geometry import box
        say(f"[ogr] bbox filter: {bbox_wgs84}")
        gdf4326 = gdf.to_crs(4326)
        mask = gdf4326.intersects(box(*bbox_wgs84))
        gdf = gdf.loc[mask].copy()
        say(f"[ogr] inside bbox: {len(gdf)}")

    try:
        line_mask = gdf["geom"].str.upper().eq("LINE")
        if line_mask.any():
            lines_df = gdf.loc[line_mask].copy()

            # Effective tolerance: if user set 0, fall back to flat_dist_precise or 0.2
            tol_user = float(line_merge_tol or 0.0)
            tol_eff  = tol_user if tol_user > 0 else max(float(flat_dist_precise or 0.0), 0.2)
            say(f"[ogr] effective line-merge tolerance (tol_eff) = {tol_eff}")

            # Try direct per-block grouping if we already have insert pose fields
            block_name_candidates = ["block_name","BlockName","BLOCKNAME","BLOCK_NAME","Block","BLOCK",
                                     "BlockRef","BlockRefName","DXF_BLOCK","DXF_BLOCK_NAME"]
            pose_candidates = ["InsertionX","InsertionY","InsertX","InsertY","BLK_X","BLK_Y"]

            blk_col = (_find_columns_case_insensitive(lines_df.columns, block_name_candidates) or [None])[0]
            pose_cols = _find_columns_case_insensitive(lines_df.columns, pose_candidates)

            merged_rows = []

            def _collect_parts(series):
                parts=[]
                for g in series:
                    if isinstance(g, LineString) and g.length>0: parts.append(g)
                    elif isinstance(g, MultiLineString):
                        parts.extend([ls for ls in g.geoms if ls.length>0])
                return parts

            if blk_col and len(pose_cols) >= 2:
                grp_cols = ["layer", blk_col] + pose_cols[:2]  # X,Y is sufficient
                say(f"[ogr] per-block merge via existing columns: {grp_cols}")
                for key, sub in lines_df.groupby(grp_cols, dropna=False):
                    parts = _collect_parts(sub.geometry)
                    if not parts:
                        continue
                    merged = _merge_lines_robust(parts, tol_eff, say) \
                             or _merge_lines_graph(parts, tol_eff) \
                             or _as_single_multiline(parts)
                    base = {c: sub.iloc[0].get(c, None) for c in sub.columns if c != "geometry"}
                    merged_rows.append({**base, "geom":"LINE", "geometry": merged})
            else:
                # Two-pass: assign lines to nearest INSERT (same name preferred)
                if inserts:
                    say("[ogr] assigning lines to nearest INSERT instance…")
                    lines_tagged = _assign_block_instance_ids(lines_df, inserts, tol_eff, on_progress=on_progress)

                    has_id = lines_tagged["_blk_id"].notna()
                    # Merge strictly within each INSERT instance
                    for blk_id, sub in lines_tagged.loc[has_id].groupby("_blk_id"):
                        parts = _collect_parts(sub.geometry)
                        if not parts: continue
                        merged = _merge_lines_robust(parts, tol_eff, say) \
                                 or _merge_lines_graph(parts, tol_eff) \
                                 or _as_single_multiline(parts)
                        base = {c: sub.iloc[0].get(c, None) for c in sub.columns if c not in ("geometry","_blk_id")}
                        merged_rows.append({**base, "geom":"LINE", "geometry": merged})

                    # Unassigned lines → conservative per-layer (no global collapse)
                    remain = lines_tagged.loc[~has_id]
                    if len(remain):
                        say(f"[ogr] lines without INSERT match: {len(remain)} → conservative per-layer merge")
                        for layer_name, sub in remain.groupby("layer"):
                            parts = _collect_parts(sub.geometry)
                            if not parts: continue
                            merged = _merge_lines_graph(parts, tol_eff) or _as_single_multiline(parts)
                            base = {c: None for c in sub.columns if c != "geometry"}
                            base["layer"] = layer_name
                            merged_rows.append({**base, "geom":"LINE", "geometry": merged})
                else:
                    # No INSERT collected → keep original segments (do NOT do global component merge)
                    say("[ogr] no INSERT found; keeping original segments (no global merge).")
                    for _, r in lines_df.iterrows():
                        merged_rows.append(dict(r))

            # Replace LINE rows with merged results
            gdf = gdf.loc[~line_mask].copy()
            if merged_rows:
                gdf = gpd.GeoDataFrame(
                    pd.concat([gdf, gpd.GeoDataFrame(merged_rows)], ignore_index=True),
                    geometry="geometry", crs=gdf.crs
                )

            say(f"[ogr] per-block line merge done (tol={line_merge_tol}) → total LINE rows: {gdf[gdf['geom'].eq('LINE')].shape[0]}")
    except Exception as ex:
        say(f"[ogr:warn] per-block (two-pass) merge failed: {ex}")

    # Reproject AFTER merging so INSERT assignment works in source units
    if target_epsg and int(target_epsg) != 4326:
        say(f"[ogr] reproject to EPSG:{int(target_epsg)}")
        gdf = gdf.to_crs(epsg=int(target_epsg))

    # Add X/Y/Z to point-like (after reprojection so XY are in target CRS)
    try:
        def _pt_coords(g):
            try:
                if getattr(g, "geom_type", "") == "Point":
                    coords = list(g.coords)[0]
                elif getattr(g, "geom_type", "") == "MultiPoint" and len(getattr(g, "geoms", [])) > 0:
                    coords = list(g.geoms[0].coords)[0]
                else:
                    c = g.centroid; coords = list(c.coords)[0]
                x = float(coords[0]); y = float(coords[1]); z = float(coords[2]) if len(coords) >= 3 else None
                return x, y, z
            except Exception:
                return None, None, None
        is_point = gdf["geom"].str.upper().eq("POINT")
        if is_point.any():
            xs, ys, zs = [], [], []
            for g in gdf.loc[is_point, "geometry"]:
                x, y, z = _pt_coords(g); xs.append(x); ys.append(y); zs.append(z)
            gdf.loc[is_point, "X"] = xs; gdf.loc[is_point, "Y"] = ys; gdf.loc[is_point, "Z"] = zs
            say(f"[ogr] POINT attributes added: X/Y/Z for {is_point.sum()} features")
    except Exception as ex:
        say(f"[ogr] add XY(Z) failed: {ex}")

    # Group to buckets
    out: Dict[Tuple[str, str], "gpd.GeoDataFrame"] = {}
    for (layer, geom), sub in gdf.groupby(["layer", "geom"]):
        say(f"[ogr] group {layer} / {geom}: {len(sub)}")
        out[(str(layer), str(geom))] = sub.reset_index(drop=True)
    say(f"[ogr] grouped buckets: {len(out)}")
    return out



def precise_convert(
    src_path: str = None,
    target_layers: List[str] = None,
    layers: List[str] = None,
    output_path: str = None,
    out_path: str = None,
    output_dir: str = None,
    driver: str = None,
    overwrite: bool = True,
    source_epsg: int = 3826,
    src_epsg: int = None,
    target_epsg: int = None,
    tgt_epsg: int = None,
    merge_tolerance: float = 0.2,      # maps to line_merge_tol
    block_mode: str = "keep-merge",
    include_3d: bool = False,
    bbox_wgs84: Optional[Tuple[float, float, float, float]] = None,
    flat_dist_precise: float = 0.2,
    on_progress: ProgressCB = None,
    **kwargs
):
    """
    UI-facing wrapper. If ezdxf is available → ezdxf pipeline; otherwise → fast OGR fallback.
    """
    dxf_paths = _coerce_paths(src_path, kwargs.get("dxf_paths"))
    if not dxf_paths:
        raise TypeError("precise_convert: missing 'src_path' or 'dxf_paths'.")

    lyr_list = target_layers if target_layers is not None else layers
    if lyr_list is not None and not isinstance(lyr_list, (list, tuple)):
        lyr_list = [str(lyr_list)]

    s_epsg = src_epsg if src_epsg is not None else source_epsg
    t_epsg = tgt_epsg if tgt_epsg is not None else target_epsg

    # Detect ezdxf availability
    ez_ok = True
    try:
        import ezdxf  # noqa: F401
    except Exception:
        ez_ok = False

    if not ez_ok:
        if on_progress:
            try: on_progress("ezdxf not available → OGR fallback (two-pass INSERT + per-instance keep-merge).")
            except: pass
        return _convert_ogr_fallback(
            dxf_paths=dxf_paths,
            source_epsg=int(s_epsg) if s_epsg else 3826,
            target_epsg=int(t_epsg) if t_epsg else None,
            target_layers=list(lyr_list) if lyr_list else None,
            bbox_wgs84=bbox_wgs84,
            line_merge_tol=float(merge_tolerance or 0.0),
            flat_dist_precise=float(flat_dist_precise or 0.2),
            on_progress=on_progress,
        )

    # ezdxf path
    return _precise_convert_ezdxf(
        dxf_paths=dxf_paths,
        source_epsg=int(s_epsg) if s_epsg else 3826,
        target_epsg=int(t_epsg) if t_epsg else None,
        include_3d=include_3d,
        flat_dist_precise=float(flat_dist_precise or 0.2),
        target_layers=list(lyr_list) if lyr_list else None,
        bbox_wgs84=bbox_wgs84,
        on_progress=on_progress,
        block_mode=block_mode or "keep-merge",
        line_merge_tol=float(merge_tolerance or 0.0),
    )


# ---------------------------------------------------------------------
# Writers (GPKG / SHP / fallbacks)
# ---------------------------------------------------------------------

def _normalize_bucket_geoms(layer_geom_key: tuple, gdf):
    """Ensure geoms match the bucket type; drop incompatible/empty ones."""
    from shapely.geometry import Point, LineString, Polygon, MultiPoint, MultiPolygon
    want = (layer_geom_key[1] or "").upper()
    if want == "LINE":
        fixed = [_extract_lineal(g) for g in gdf.geometry]
        gdf = gdf.copy(); gdf["geometry"] = fixed
        return gdf[~gdf.geometry.isna() & ~gdf.geometry.is_empty]
    elif want == "POLYGON":
        mask = gdf.geometry.apply(lambda g: isinstance(g, (Polygon, MultiPolygon)) and g and not g.is_empty)
        return gdf.loc[mask].copy()
    elif want == "POINT":
        mask = gdf.geometry.apply(lambda g: isinstance(g, (Point, MultiPoint)) and g and not g.is_empty)
        return gdf.loc[mask].copy()
    return gdf


def write_outputs(
    buckets: Dict[Tuple[str, str], "gpd.GeoDataFrame"],
    out_path: str,
    driver: str,
    overwrite: bool = False,
    on_progress: ProgressCB = None,
):
    """
    Write grouped GeoDataFrames to:
      - GPKG: one file with multiple layers
      - ESRI Shapefile: <Layer>_<Geom>.shp per bucket
      - Fallbacks: pyshp or GeoJSON
    """
    written = []
    import os
    from pathlib import Path

    def say(m):
        if on_progress:
            try: on_progress(m)
            except Exception: pass

    if not buckets:
        say("[write] empty buckets")
        return written

    # GPKG
    if (driver and driver.upper() == "GPKG") or (out_path and out_path.lower().endswith(".gpkg")):
        gpkg = out_path if out_path.lower().endswith(".gpkg") else (out_path.rstrip("\\/") + "\\bundle.gpkg")
        try:
            import geopandas as gpd  # noqa
        except Exception as ex:
            say(f"[write:error] GeoPandas/Fiona required for GPKG: {ex}")
            return written
        if overwrite and os.path.exists(gpkg):
            try: os.remove(gpkg)
            except: pass
        for (layer, geom), gdf in buckets.items():
            try:
                lname = str(layer)
                gdf = _normalize_bucket_geoms((layer, geom), gdf)
                if gdf.empty:
                    say(f"[write:skip] {layer}/{geom} empty after normalize"); continue
                gdf = _sanitize_fields_for_driver(gdf, "GPKG")
                gdf.to_file(gpkg, layer=lname, driver="GPKG")
                written.append({"path": gpkg, "layer": lname, "count": int(len(gdf))})
                say(f"[write] GPKG: {lname} ({len(gdf)}) → {gpkg}")
            except Exception as ex:
                say(f"[write:error] GPKG layer {layer} failed: {ex}")
        return written

    # ESRI Shapefile
    os.makedirs(out_path, exist_ok=True)
    ok_any = False
    errors = []
    for (layer, geom), gdf in buckets.items():
        safe_layer = _sanitize_filename(layer)
        safe_geom = _sanitize_filename(geom)
        fpath = str(Path(out_path) / f"{safe_layer}_{safe_geom}.shp")
        if overwrite:
            for ext in (".shp",".shx",".dbf",".cpg",".prj",".qpj"):
                try: os.remove(os.path.splitext(fpath)[0]+ext)
                except: pass
        try:
            gdf = _normalize_bucket_geoms((layer, geom), gdf)
            if gdf.empty:
                say(f"[write:skip] {layer}/{geom} empty after normalize"); continue
            gdf.to_file(fpath, driver="ESRI Shapefile")
            written.append({"path": fpath, "layer": layer, "count": int(len(gdf))})
            say(f"[write] SHP: {layer} ({len(gdf)}) → {fpath}")
            ok_any = True
        except Exception as ex:
            errors.append((fpath, ex))
            say(f"[write:warn] Fiona failed, will try pyshp/GeoJSON: {fpath} → {ex}")

    if ok_any:
        return written

    # pyshp fallback
    try:
        import shapefile as pyshp
        from shapely.geometry import mapping as shp_mapping
        say("[write] using pyshp fallback")

        def _short(n:str) -> str:
            s=_sanitize_filename(n).upper()
            return s[:10] if len(s)>10 else s

        def _shapeType(gt:str):
            u = (gt or "").upper()
            return pyshp.POINT if u=="POINT" else pyshp.POLYLINE if u=="LINE" else pyshp.POLYGON if u=="POLYGON" else pyshp.NULL

        for (layer, geom), gdf in buckets.items():
            gdf = _normalize_bucket_geoms((layer, geom), gdf)
            if gdf.empty:
                say(f"[write:skip] {layer}/{geom} empty after normalize"); continue

            shp_type = _shapeType(geom)
            if shp_type == pyshp.NULL:
                say(f"[write:warn] unsupported geom: {geom}"); continue

            safe_layer = _sanitize_filename(layer)
            safe_geom = _sanitize_filename(geom)
            fpath = str(Path(out_path) / f"{safe_layer}_{safe_geom}_pyshp.shp")

            cols = list(gdf.columns)
            if "geometry" in cols:
                cols.remove("geometry")

            preferred = ["block_name","BLK_X","BLK_Y","BLK_Z","BLK_ROT","BLK_SX","BLK_SY","BLK_SZ","X","Y","Z"]
            ordered = preferred + [c for c in cols if c not in preferred]

            w = pyshp.Writer(fpath, shp_type)
            w.field(_short("FID"), "N", 18, 0)
            for c in ordered:
                try:
                    if c in gdf.select_dtypes(include=["int","float"]).columns:
                        w.field(_short(c), "F", 19, 6)
                    else:
                        w.field(_short(c), "C", 254)
                except Exception:
                    pass

            for i, row in gdf.reset_index(drop=True).iterrows():
                g = shp_mapping(row.geometry)
                if shp_type == pyshp.POINT:
                    x, y = g["coordinates"][:2]; w.point(x, y)
                elif shp_type == pyshp.POLYLINE:
                    coords = list(g["coordinates"])
                    parts = [list(ls) for ls in coords] if g["type"] == "MultiLineString" else [coords]
                    w.line(parts)
                elif shp_type == pyshp.POLYGON:
                    if g["type"] == "MultiPolygon":
                        parts = [list(ring) for poly in g["coordinates"] for ring in poly]
                    else:
                        parts = [list(r) for r in g["coordinates"]]
                    w.poly(parts)

                rec = [int(i)]
                for c in ordered:
                    try:
                        v = row.get(c, None)
                        if v is None:
                            rec.append("")
                        elif isinstance(v, (int, float)):
                            rec.append(v)
                        else:
                            s = str(v); rec.append(s if len(s) <= 254 else s[:254])
                    except Exception:
                        rec.append("")
                while len(rec) < len(w.fields) - 1:
                    rec.append("")
                w.record(*rec)
            w.close()

            written.append({"path": fpath, "layer": layer, "count": int(len(gdf))})
            say(f"[write] pyshp: {layer} ({len(gdf)}) → {fpath}")

        if written:
            return written
    except Exception as ex:
        say(f"[write:warn] pyshp unavailable or failed: {ex}, fallback to GeoJSON")

    # GeoJSON fallback
    say("[write] using GeoJSON fallback")
    for (layer, geom), gdf in buckets.items():
        gdf = _normalize_bucket_geoms((layer, geom), gdf)
        if gdf.empty:
            say(f"[write:skip] {layer}/{geom} empty after normalize"); continue
        safe_layer = _sanitize_filename(layer)
        safe_geom = _sanitize_filename(geom)
        fpath = str(Path(out_path) / f"{safe_layer}_{safe_geom}.geojson")
        try:
            feats = []
            for i, row in gdf.reset_index(drop=True).iterrows():
                from shapely.geometry import mapping as shp_mapping2
                feat = {"type":"Feature","properties":{}, "geometry": shp_mapping2(row.geometry)}
                for k, v in row.items():
                    if k == "geometry": continue
                    feat["properties"][k] = None if v is None else (float(v) if isinstance(v,(int,float)) else str(v))
                feats.append(feat)
            with open(fpath, "w", encoding="utf-8") as f:
                import json; json.dump({"type":"FeatureCollection","features":feats}, f, ensure_ascii=False)
            written.append({"path": fpath, "layer": layer, "count": int(len(feats))})
            say(f"[write] GeoJSON: {layer} ({len(feats)}) → {fpath}")
        except Exception as ex:
            say(f"[write:error] GeoJSON failed: {fpath} → {ex}")
    return written
