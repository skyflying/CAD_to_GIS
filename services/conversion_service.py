# -*- coding: utf-8 -*-
"""
CAD (DXF) -> GIS conversion engine shared by:
  - CAD_to_GIS (standalone PySide6 app)
  - CAD_to_GIS_QGIS_Plugin

Two pipelines with the same output contract  {(layer, geom): GeoDataFrame}:
  - ezdxf  (preferred)
  - GDAL/OGR DXF driver (fallback when ezdxf is not installed)

geom is one of "LINE", "POLYGON", "POINT".
"""

from __future__ import annotations
from typing import Tuple, Dict, List, Optional, Callable, Iterator
from contextlib import contextmanager
import math
import os
import re
import pandas as pd

ProgressCB = Optional[Callable[[str], None]]

CURVE_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE"}
FACE_TYPES = {"3DFACE", "THREE_D_FACE", "SOLID", "TRACE"}
MAX_BLOCK_DEPTH = 16


def _make_say(on_progress: ProgressCB):
    def say(m):
        if on_progress:
            try:
                on_progress(m)
            except Exception:
                pass
    return say


# ---------------------------------------------------------------------
# Names / paths
# ---------------------------------------------------------------------

_WIN_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _sanitize_filename(name: str) -> str:
    """Sanitize a name for file names / OGR layer names.

    Keeps Unicode (e.g. Chinese CAD layer names); only removes characters that
    are illegal on Windows. The old version replaced every non-ASCII char with
    "_", so layers like 管線 / 電纜 all became "__" and overwrote each other.
    """
    s = "layer" if name is None else str(name)
    s = _WIN_ILLEGAL.sub("_", s).strip(" .")
    if not s:
        s = "layer"
    if s.upper() in _RESERVED:
        s = f"_{s}_"
    return s[:100]


def _unique_name(base: str, used: set) -> str:
    name, i = base, 2
    while name.lower() in used:
        name = f"{base}_{i}"
        i += 1
    used.add(name.lower())
    return name


def _sanitize_fields_for_driver(df, driver: str):
    """GPKG: a data column called 'geom' clashes with the geometry column name."""
    drv = (driver or "").upper()
    geom_col_name = df.geometry.name if hasattr(df, "geometry") else "geometry"
    rename_map = {}
    if drv == "GPKG":
        for col in list(df.columns):
            if col == geom_col_name:
                continue
            if str(col).lower() == "geom":
                new_name, i = "geom_type", 1
                while new_name in df.columns or new_name == geom_col_name:
                    new_name = f"geom_type_{i}"
                    i += 1
                rename_map[col] = new_name
    return df.rename(columns=rename_map) if rename_map else df


def _coerce_paths(src_path, dxf_paths_kw=None) -> List[str]:
    """Normalize incoming path(s): string, list, or stringified list -> list[str]."""
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

    seen, norm = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            norm.append(p)
    return norm


# ---------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------

def _coords_to_geom(coords: List[tuple]):
    """Convert coordinate sequence into (geom_type, shapely geom)."""
    from shapely.geometry import Point, LineString, Polygon
    xy = [(c[0], c[1]) for c in coords]
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
        lines = [g for g in geom.geoms
                 if isinstance(g, (LineString, MultiLineString)) and not g.is_empty]
        if not lines:
            return None
        try:
            m = linemerge(unary_union(lines))
            return m if m is not None and not m.is_empty else None
        except Exception:
            flat = []
            for g in lines:
                if isinstance(g, LineString):
                    flat.append(g)
                else:
                    flat.extend([ls for ls in g.geoms if not ls.is_empty])
            return MultiLineString(flat) if flat else None
    return None


def _as_single_multiline(parts):
    """Pack list[LineString] -> one MultiLineString (None if nothing usable)."""
    from shapely.geometry import MultiLineString
    good = [ls for ls in parts if ls is not None and not ls.is_empty]
    return MultiLineString(good) if good else None


def _grid_snap_lines(lines, tol: float):
    """Snap endpoints to a tolerance grid to improve merging; mid-points kept as-is."""
    from shapely.geometry import LineString
    if tol <= 0 or not lines:
        return lines

    def q(x):
        return round(x / tol) * tol

    out = []
    for ls in lines:
        try:
            coords = [(c[0], c[1]) for c in ls.coords]
            if len(coords) < 2:
                continue
            new = [(q(coords[0][0]), q(coords[0][1]))] + coords[1:-1] + [(q(coords[-1][0]), q(coords[-1][1]))]
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
    """tol <= 0: exact endpoint merge; tol > 0: endpoint grid-snap, then merge."""
    from shapely.ops import unary_union, linemerge
    if not lines:
        return None
    try:
        L1 = _grid_snap_lines(lines, tol) if tol > 0 else lines
        if not L1:
            return None
        m1 = linemerge(unary_union(L1))
        if m1 is not None and not m1.is_empty:
            return m1
    except Exception as ex:
        say(f"[merge] robust exception: {ex}")
    return None


def _merge_lines_graph(lines, tol: float):
    """Greedy graph stitcher (endpoints bucketed by tolerance)."""
    from shapely.geometry import LineString, MultiLineString
    from collections import defaultdict
    if not lines:
        return None

    def q(x):
        return round(x / tol) * tol if tol > 0 else x

    edges = []
    for ls in lines:
        try:
            coords = [(c[0], c[1]) for c in ls.coords]
            if len(coords) < 2:
                continue
            a = (q(coords[0][0]), q(coords[0][1]))
            b = (q(coords[-1][0]), q(coords[-1][1]))
            edges.append((a, b, coords))
        except Exception:
            continue
    if not edges:
        return None

    node_deg = defaultdict(int)
    adj = defaultdict(list)
    for i, (a, b, _c) in enumerate(edges):
        adj[a].append(i)
        adj[b].append(i)
        node_deg[a] += 1
        node_deg[b] += 1
    used = [False] * len(edges)

    def walk(start_node, first_edge=None):
        path, cur = [], start_node
        ei = first_edge
        while True:
            if ei is None:
                ei = next((e for e in adj[cur] if not used[e]), None)
                if ei is None:
                    break
            used[ei] = True
            a, b, coords = edges[ei]
            if a == cur:
                seg, other = coords, b
            else:
                seg, other = list(reversed(coords)), a
            if not path:
                path.extend(seg)
            else:
                path.extend(seg[1:] if path[-1] == seg[0] else seg)
            cur, ei = other, None
            if node_deg[cur] != 2:
                break
        return path

    merged = []
    for node, deg in list(node_deg.items()):
        if deg != 2:
            while any(not used[e] for e in adj[node]):
                coords = walk(node)
                if len(coords) >= 2:
                    merged.append(LineString(coords))
    # remaining = closed loops
    for ei in range(len(edges)):
        if not used[ei]:
            a, _b, _c = edges[ei]
            coords = walk(a, ei)
            if len(coords) >= 2:
                merged.append(LineString(coords))

    merged = [ls for ls in merged if ls.length > 0]
    if not merged:
        return None
    return merged[0] if len(merged) == 1 else MultiLineString(merged)


def _merge_lines(lines, tol: float, say=lambda m: None):
    return (_merge_lines_robust(lines, tol, say)
            or _merge_lines_graph(lines, tol)
            or _as_single_multiline(lines))


def _even_odd_polygon(rings: List[List[tuple]]):
    """Build a (Multi)Polygon from boundary rings using even-odd fill (hatch holes)."""
    from shapely.geometry import Polygon
    polys = []
    for r in rings:
        xy = [(p[0], p[1]) for p in r]
        if len(xy) < 3:
            continue
        try:
            pg = Polygon(xy)
            if not pg.is_valid:
                pg = pg.buffer(0)
            if not pg.is_empty and pg.area > 0:
                polys.append(pg)
        except Exception:
            continue
    if not polys:
        return None
    res = polys[0]
    for pg in polys[1:]:
        try:
            res = res.symmetric_difference(pg)
        except Exception:
            res = res.union(pg)
    return res if not res.is_empty else None


# ---------------------------------------------------------------------
# ezdxf pipeline
# ---------------------------------------------------------------------

def _flatten_with_path(e, dist: float, include_3d: bool = False) -> List[tuple]:
    from ezdxf import path as ezpath
    p = ezpath.make_path(e)
    return [(v.x, v.y, v.z if include_3d else 0.0) for v in p.flattening(distance=dist)]


def _fallback_polyline(e, include_3d: bool) -> List[tuple]:
    pts = []
    if hasattr(e, "get_points"):
        for v in e.get_points("xy"):
            pts.append((v[0], v[1], 0.0))
        if getattr(e, "closed", False) and pts and pts[0] != pts[-1]:
            pts.append(pts[0])
    else:
        for v in e.vertices:
            loc = v.dxf.location
            pts.append((loc.x, loc.y, loc.z if include_3d else 0.0))
        if getattr(e, "is_closed", False) and pts and pts[0] != pts[-1]:
            pts.append(pts[0])
    return pts


def _fallback_circle(e, dist: float) -> List[tuple]:
    c = e.dxf.center
    r = float(e.dxf.radius)
    segs = max(24, int(2 * math.pi * r / max(dist, 0.01)))
    segs = min(segs, 3600)
    return [(c.x + r * math.cos(2 * math.pi * i / segs),
             c.y + r * math.sin(2 * math.pi * i / segs), 0.0) for i in range(segs + 1)]


def _fallback_arc(e, dist: float) -> List[tuple]:
    """CAD arcs run counter-clockwise from start to end angle.

    The old version swapped the angles when end < start, which drew the
    complementary (wrong) arc for arcs crossing 0 deg (e.g. 350 -> 10).
    """
    c = e.dxf.center
    r = float(e.dxf.radius)
    a1 = math.radians(float(e.dxf.start_angle))
    a2 = math.radians(float(e.dxf.end_angle))
    while a2 <= a1:
        a2 += 2 * math.pi
    sweep = a2 - a1
    steps = max(8, min(3600, int(sweep * r / max(dist, 0.01))))
    return [(c.x + r * math.cos(a1 + sweep * i / steps),
             c.y + r * math.sin(a1 + sweep * i / steps), 0.0) for i in range(steps + 1)]


def _curve_points(e, include_3d: bool, dist: float) -> List[tuple]:
    t = e.dxftype()
    try:
        return _flatten_with_path(e, dist, include_3d)
    except Exception:
        if t == "LINE":
            s, ed = e.dxf.start, e.dxf.end
            return [(s.x, s.y, s.z if include_3d else 0.0), (ed.x, ed.y, ed.z if include_3d else 0.0)]
        if t in {"LWPOLYLINE", "POLYLINE"}:
            return _fallback_polyline(e, include_3d)
        if t == "CIRCLE":
            return _fallback_circle(e, dist)
        if t == "ARC":
            return _fallback_arc(e, dist)
    return []


def _hatch_polygon(e, dist: float):
    """HATCH -> (Multi)Polygon. Supports polyline AND edge boundary paths, with holes.

    The old version read `e.paths.polygons` (does not exist in ezdxf) and then only
    edge paths, so hatches with polyline boundaries were silently dropped.
    """
    rings: List[List[tuple]] = []
    try:
        from ezdxf import path as ezpath
        for p in ezpath.from_hatch(e):
            pts = [(v.x, v.y) for v in p.flattening(distance=dist)]
            if len(pts) >= 3:
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
                rings.append(pts)
    except Exception:
        rings = []
    if not rings:
        try:
            for bp in e.paths:
                ring = []
                if hasattr(bp, "vertices"):
                    ring = [(v[0], v[1]) for v in bp.vertices]
                elif hasattr(bp, "edges"):
                    for edge in bp.edges:
                        if hasattr(edge, "start"):
                            ring.append((edge.start[0], edge.start[1]))
                if len(ring) >= 3:
                    if ring[0] != ring[-1]:
                        ring.append(ring[0])
                    rings.append(ring)
        except Exception:
            pass
    return _even_odd_polygon(rings)


def _face_polygon(e):
    from shapely.geometry import Polygon
    t = e.dxftype()
    v = [e.dxf.vtx0, e.dxf.vtx1, e.dxf.vtx2, e.dxf.vtx3]
    if t in {"SOLID", "TRACE"}:  # SOLID vertex order is 0-1-3-2
        v = [v[0], v[1], v[3], v[2]]
    xy = []
    for p in v:
        q = (p.x, p.y)
        if not xy or xy[-1] != q:
            xy.append(q)
    if len(xy) >= 3:
        pg = Polygon(xy)
        if not pg.is_valid:
            pg = pg.buffer(0)
        return pg if not pg.is_empty else None
    return None


def _precise_rows_from_entity(e, layer, include_3d, dist) -> List[dict]:
    from shapely.geometry import Point as _Pt
    rows = []
    t = e.dxftype()
    if t in CURVE_TYPES:
        pts = _curve_points(e, include_3d, dist)
        if pts:
            gtype, geom = _coords_to_geom(pts)
            if gtype and geom is not None and not geom.is_empty:
                rows.append({"layer": layer, "geom": gtype, "geometry": geom})
    elif t == "POINT":
        p = e.dxf.location
        rows.append({"layer": layer, "geom": "POINT", "geometry": _Pt(p.x, p.y)})
    elif t == "HATCH":
        pg = _hatch_polygon(e, dist)
        if pg is not None:
            rows.append({"layer": layer, "geom": "POLYGON", "geometry": pg})
    elif t in FACE_TYPES:
        try:
            pg = _face_polygon(e)
            if pg is not None:
                rows.append({"layer": layer, "geom": "POLYGON", "geometry": pg})
        except Exception:
            pass
    return rows


def _extract_block_meta(e) -> dict:
    """Extract block metadata from INSERT (name, pose, attributes)."""
    meta = {}
    try:
        meta["block_name"] = str(getattr(e.dxf, "name", None) or "")
    except Exception:
        meta["block_name"] = ""
    try:
        ip = e.dxf.insert
        meta["BLK_X"] = float(ip.x)
        meta["BLK_Y"] = float(ip.y)
        meta["BLK_Z"] = float(ip.z)
    except Exception:
        pass
    for k_src, k_out in [("rotation", "BLK_ROT"), ("xscale", "BLK_SX"), ("yscale", "BLK_SY"), ("zscale", "BLK_SZ")]:
        try:
            meta[k_out] = float(e.dxf.get(k_src))
        except Exception:
            pass
    try:
        for a in (getattr(e, "attribs", None) or []):
            try:
                tag = str(a.dxf.tag).strip()
                if not tag:
                    continue
                key = "ATT_" + re.sub(r"[^0-9A-Za-z_]+", "_", tag.upper())
                meta[key] = "" if a.dxf.text is None else str(a.dxf.text)
            except Exception:
                continue
    except Exception:
        pass
    return meta


def _iter_block_children(insert, parent_layer: str, say, depth: int = 0) -> Iterator[tuple]:
    """Yield (entity, effective_layer) for every primitive inside an INSERT.

    Fixes vs old version:
      * nested INSERTs are expanded recursively (they were silently dropped);
      * entities on layer "0" inside a block inherit the INSERT's layer, as in
        AutoCAD (ezdxf's virtual_entities() keeps them on "0").
    """
    try:
        children = list(insert.virtual_entities())
    except Exception as ex:
        say(f"[warn] virtual_entities failed ({getattr(insert.dxf, 'name', '?')}): {ex}")
        return
    for se in children:
        sl = getattr(se.dxf, "layer", "0") or "0"
        eff = parent_layer if sl == "0" else sl
        if se.dxftype() == "INSERT":
            if depth + 1 < MAX_BLOCK_DEPTH:
                yield from _iter_block_children(se, eff, say, depth + 1)
        else:
            yield se, eff


def _keep_merge_rows(e, layer, meta, include_3d, dist, tol, say) -> List[dict]:
    """One INSERT -> at most one LINE + one POLYGON + one POINT row (block kept as a unit)."""
    from shapely.geometry import LineString, Point, MultiPoint
    from shapely.ops import unary_union

    lines, polys, pts = [], [], []
    for se, _eff in _iter_block_children(e, layer, say):
        st = se.dxftype()
        try:
            if st in CURVE_TYPES:
                xy = [(x, y) for x, y, _z in _curve_points(se, include_3d, dist)]
                # NOTE: the old code dropped the closing vertex here, which left a
                # gap in every circle / closed polyline inside a block.
                if len(xy) >= 2:
                    ls = LineString(xy)
                    if ls.length > 0:
                        lines.append(ls)
            else:
                for r in _precise_rows_from_entity(se, layer, include_3d, dist):
                    (polys if r["geom"] == "POLYGON" else pts if r["geom"] == "POINT" else lines).append(r["geometry"])
        except Exception as ex:
            say(f"[warn] block child {st} failed: {ex}")

    rows = []
    if lines:
        merged = _merge_lines(lines, tol, say)
        if merged is not None and not merged.is_empty:
            rows.append({"layer": layer, "geom": "LINE", "geometry": merged, **meta})
    if polys:
        try:
            pu = unary_union(polys)
        except Exception:
            pu = None
        if pu is not None and not pu.is_empty:
            rows.append({"layer": layer, "geom": "POLYGON", "geometry": pu, **meta})
    if pts:
        rows.append({"layer": layer, "geom": "POINT",
                     "geometry": pts[0] if len(pts) == 1 else MultiPoint(pts), **meta})
    if not rows:  # empty / text-only block: keep the reference itself
        ip = e.dxf.insert
        rows.append({"layer": layer, "geom": "POINT", "geometry": Point(ip.x, ip.y), **meta})
    return rows


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
    import ezdxf
    from shapely.geometry import Point as _Pt

    say = _make_say(on_progress)
    sel_layers = set(target_layers) if target_layers else None
    mode = (block_mode or "keep-merge").lower().strip()
    keep = mode in ("keep-merge", "keep-merge-per")
    tol = float(line_merge_tol or 0.0)
    say(f"[convert] srcEPSG={source_epsg} tgtEPSG={target_epsg} bbox={bbox_wgs84} "
        f"include_3d={include_3d} block_mode={mode} merge_tol={tol}")

    rows: List[dict] = []
    for path in dxf_paths:
        try:
            say(f"[convert] read: {path}")
            doc = ezdxf.readfile(path)
            msp = doc.modelspace()
        except Exception as ex:
            say(f"[error] read failed: {ex}")
            continue

        count = ins_count = 0
        for e in msp:
            count += 1
            if count % 1000 == 0:
                say(f"[convert] processing… {count} entities")
            try:
                t = e.dxftype()
                layer = getattr(e.dxf, "layer", "0") or "0"

                if t == "INSERT":
                    ins_count += 1
                    if ins_count % 50 == 0:
                        say(f"[convert] …INSERT expanded: {ins_count}")
                    meta = _extract_block_meta(e)

                    if keep:
                        # block is kept as one unit on the INSERT's own layer
                        if sel_layers and layer not in sel_layers:
                            continue
                        rows += _keep_merge_rows(e, layer, meta, include_3d, flat_dist_precise, tol, say)
                        continue

                    # explode: every child goes to its own (effective) layer.
                    # The INSERT's own layer is NOT used as a filter here, otherwise
                    # selecting a layer that only exists inside blocks returned nothing.
                    expanded = False
                    for se, eff in _iter_block_children(e, layer, say):
                        if sel_layers and eff not in sel_layers:
                            continue
                        rws = _precise_rows_from_entity(se, eff, include_3d, flat_dist_precise)
                        for r in rws:
                            r.update(meta)
                        rows += rws
                        expanded = expanded or bool(rws)
                    if not expanded and (not sel_layers or layer in sel_layers):
                        ip = e.dxf.insert
                        rows.append({"layer": layer, "geom": "POINT", "geometry": _Pt(ip.x, ip.y), **meta})
                    continue

                if sel_layers and layer not in sel_layers:
                    continue
                rows += _precise_rows_from_entity(e, layer, include_3d, flat_dist_precise)
            except Exception as ex:
                say(f"[warn] entity failed: {ex}")
                continue

        say(f"[convert] done file: {path}, total rows {len(rows)}")

    if not rows:
        say("[convert] no rows")
        return {}

    import geopandas as gpd
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=f"EPSG:{source_epsg or 4326}")
    return _finalize(gdf, source_epsg, target_epsg, bbox_wgs84, say, tag="convert")


# ---------------------------------------------------------------------
# Shared post-processing (both pipelines)
# ---------------------------------------------------------------------

def _finalize(gdf, source_epsg, target_epsg, bbox_wgs84, say, tag="convert"):
    """bbox filter -> reproject -> X/Y/Z for points -> group into buckets.

    The ezdxf pipeline previously skipped this entirely (target EPSG and bbox
    were ignored), and the OGR pipeline never reprojected to EPSG:4326.
    """
    if bbox_wgs84 and isinstance(bbox_wgs84, (list, tuple)) and len(bbox_wgs84) == 4:
        from shapely.geometry import box
        say(f"[{tag}] bbox filter (WGS84): {bbox_wgs84}")
        mask = gdf.to_crs(4326).intersects(box(*bbox_wgs84))
        gdf = gdf.loc[mask].copy()
        say(f"[{tag}] inside bbox: {len(gdf)}")

    if target_epsg and int(target_epsg) != int(source_epsg or 4326):
        say(f"[{tag}] reproject EPSG:{source_epsg} → EPSG:{int(target_epsg)}")
        gdf = gdf.to_crs(epsg=int(target_epsg))

    try:
        is_point = gdf["geom"].astype(str).str.upper().eq("POINT")
        if is_point.any():
            xs, ys, zs = [], [], []
            for g in gdf.loc[is_point, "geometry"]:
                try:
                    p = g if g.geom_type == "Point" else (g.geoms[0] if g.geom_type == "MultiPoint" else g.centroid)
                    xs.append(float(p.x))
                    ys.append(float(p.y))
                    zs.append(float(p.z) if p.has_z else None)
                except Exception:
                    xs.append(None); ys.append(None); zs.append(None)
            gdf.loc[is_point, "X"] = xs
            gdf.loc[is_point, "Y"] = ys
            if any(z is not None for z in zs):
                gdf.loc[is_point, "Z"] = zs
    except Exception as ex:
        say(f"[{tag}] add XY(Z) failed: {ex}")

    out: Dict[Tuple[str, str], "gpd.GeoDataFrame"] = {}
    for (layer, geom), sub in gdf.groupby(["layer", "geom"]):
        say(f"[{tag}] group {layer} / {geom}: {len(sub)}")
        out[(str(layer), str(geom))] = sub.reset_index(drop=True)
    say(f"[{tag}] grouped buckets: {len(out)}")
    return out


# ---------------------------------------------------------------------
# GDAL/OGR pipeline (fallback)
# ---------------------------------------------------------------------

@contextmanager
def _gdal_config(opts: Dict[str, Optional[str]]):
    """Set GDAL config options temporarily and restore them afterwards.

    The old code changed them globally (and called ogr.UseExceptions()), which
    inside QGIS also changes how QGIS itself opens DXF files.
    """
    from osgeo import gdal
    old = {k: gdal.GetConfigOption(k) for k in opts}
    try:
        for k, v in opts.items():
            gdal.SetConfigOption(k, v)
        yield
    finally:
        for k, v in old.items():
            gdal.SetConfigOption(k, v)


def _dxf_encoding_hint(path: str) -> Optional[str]:
    """DXF R2007+ (AC1021+) is always UTF-8, but GDAL decodes by $DWGCODEPAGE
    (often ANSI_1252) and garbles Chinese layer names. Return 'UTF-8' for those."""
    try:
        with open(path, "rb") as f:
            head = f.read(256 * 1024)
        if head.startswith(b"AutoCAD Binary DXF"):
            return None
        lines = [l.strip() for l in head.decode("latin-1", "ignore").splitlines()]
        for i, l in enumerate(lines[:-2]):
            if l == "$ACADVER":
                ver = lines[i + 2].upper()
                return "UTF-8" if ver >= "AC1021" else None
    except Exception:
        pass
    return None


def _collapse_gc_preserve_multis(sgeom, tol: float = 0.0, say=lambda m: None):
    """Split one feature's geometry into LINE / POLYGON / POINT parts."""
    from shapely.geometry import (Point, MultiPoint, LineString, MultiLineString,
                                  Polygon, MultiPolygon, GeometryCollection)
    from shapely.ops import unary_union

    pts, lines, polys = [], [], []

    def visit(g):
        if g is None or g.is_empty:
            return
        if isinstance(g, Point):
            pts.append(g)
        elif isinstance(g, MultiPoint):
            pts.extend(g.geoms)
        elif isinstance(g, LineString):
            if g.length > 0:
                lines.append(g)
        elif isinstance(g, MultiLineString):
            lines.extend([ls for ls in g.geoms if ls.length > 0])
        elif isinstance(g, Polygon):
            polys.append(g)
        elif isinstance(g, MultiPolygon):
            polys.extend(g.geoms)
        elif isinstance(g, GeometryCollection):
            for c in g.geoms:
                visit(c)

    visit(sgeom)
    out = {}
    if lines:
        out["LINE"] = lines[0] if len(lines) == 1 else _merge_lines(lines, tol, say)
    if polys:
        try:
            u = unary_union(polys)
        except Exception:
            u = MultiPolygon([p for p in polys if not p.is_empty])
        if u is not None and not u.is_empty:
            out["POLYGON"] = u
    if pts:
        out["POINT"] = pts[0] if len(pts) == 1 else MultiPoint(pts)
    return out


def _ogr_collect_inserts(path: str, encoding: Optional[str]) -> Dict[str, dict]:
    """No-inline pass: INSERT pose + attributes keyed by EntityHandle.

    The old version compared GetGeometryType() with 1/4, but DXF points are 3D
    (wkbPoint25D), so no INSERT was ever found.
    """
    from osgeo import ogr
    out: Dict[str, dict] = {}
    opts = {"DXF_INLINE_BLOCKS": "FALSE"}
    if encoding:
        opts["DXF_ENCODING"] = encoding
    with _gdal_config(opts):
        ds = ogr.Open(path, 0)
        if ds is None:
            return out
        lyr = ds.GetLayerByName("entities")
        if lyr is None:
            return out
        defn = lyr.GetLayerDefn()
        flds = {defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())}
        if "BlockName" not in flds:
            return out
        for f in lyr:
            try:
                bname = f.GetField("BlockName")
                if not bname:
                    continue
                g = f.GetGeometryRef()
                if g is None or ogr.GT_Flatten(g.GetGeometryType()) != ogr.wkbPoint:
                    continue
                meta = {"block_name": str(bname),
                        "BLK_X": float(g.GetX()), "BLK_Y": float(g.GetY()), "BLK_Z": float(g.GetZ())}
                if "BlockAngle" in flds and f.GetField("BlockAngle") is not None:
                    meta["BLK_ROT"] = float(f.GetField("BlockAngle"))
                if "BlockScale" in flds:
                    sc = f.GetField("BlockScale")
                    if sc:
                        for k, v in zip(("BLK_SX", "BLK_SY", "BLK_SZ"), sc):
                            meta[k] = float(v)
                if "BlockAttributes" in flds:
                    for item in (f.GetField("BlockAttributes") or []):
                        tag, _, val = str(item).partition(" ")
                        if tag:
                            meta["ATT_" + re.sub(r"[^0-9A-Za-z_]+", "_", tag.upper())] = val
                h = f.GetField("EntityHandle")
                if h:
                    out[str(h)] = meta
            except Exception:
                continue
        ds = None
    return out


def _convert_ogr_fallback(
    dxf_paths: List[str],
    *,
    source_epsg: int = 3826,
    target_epsg: Optional[int] = None,
    target_layers: Optional[List[str]] = None,
    bbox_wgs84: Optional[Tuple[float, float, float, float]] = None,
    line_merge_tol: float = 0.2,
    flat_dist_precise: float = 0.2,
    block_mode: str = "keep-merge",
    on_progress: ProgressCB = None,
) -> Dict[Tuple[str, str], "gpd.GeoDataFrame"]:
    """GDAL DXF driver pipeline.

    keep-merge: DXF_INLINE_BLOCKS=TRUE + DXF_MERGE_BLOCK_GEOMETRIES=TRUE already
    gives ONE feature per INSERT (a GeometryCollection on the INSERT's layer), so
    no nearest-INSERT guessing is needed. The old two-pass "assign lines to the
    nearest INSERT" logic could not work (inlined features carry no BlockName) and,
    when it ran, merged every plain line of a layer into a single feature.
    explode: DXF_MERGE_BLOCK_GEOMETRIES=FALSE -> one feature per block child.
    Block name / pose / attributes are attached by EntityHandle from a no-inline pass.
    """
    say = _make_say(on_progress)
    try:
        from osgeo import ogr
    except Exception as ex:
        raise RuntimeError(f"OGR fallback requested but GDAL/OGR is not available: {ex}")

    import geopandas as gpd
    from shapely import wkb as _wkb

    keep = (block_mode or "keep-merge").lower().startswith("keep")
    tol = float(line_merge_tol or 0.0)
    sel_layers = set(target_layers) if target_layers else None
    rows: List[dict] = []

    for path in dxf_paths:
        enc = _dxf_encoding_hint(path)
        say(f"[ogr] read: {path}  (mode={'keep-merge' if keep else 'explode'}, encoding={enc or 'auto'})")
        inserts = _ogr_collect_inserts(path, enc)
        say(f"[ogr] INSERT instances: {len(inserts)}")

        opts = {
            "DXF_INLINE_BLOCKS": "TRUE",
            "DXF_MERGE_BLOCK_GEOMETRIES": "TRUE" if keep else "FALSE",
            "DXF_CLOSED_LINE_AS_POLYGON": "TRUE",   # same as ezdxf pipeline
            # OGR_ARC_STEPSIZE is an angle in degrees (GDAL default 4)
            "OGR_ARC_STEPSIZE": str(max(0.5, min(5.0, float(flat_dist_precise or 0.2)))),
        }
        if enc:
            opts["DXF_ENCODING"] = enc

        with _gdal_config(opts):
            ds = ogr.Open(path, 0)
            if ds is None:
                say(f"[ogr:error] cannot open: {path}")
                continue
            lyr = ds.GetLayerByName("entities") or (ds.GetLayer(0) if ds.GetLayerCount() else None)
            if lyr is None:
                say(f"[ogr:warn] no 'entities' layer in: {path}")
                continue
            defn = lyr.GetLayerDefn()
            field_names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]

            total = kept = 0
            for f in lyr:
                total += 1
                try:
                    layer_name = (f.GetField("Layer") if "Layer" in field_names else None) or "0"
                    if sel_layers and layer_name not in sel_layers:
                        continue
                    sub = str(f.GetField("SubClasses") or "") if "SubClasses" in field_names else ""
                    if "AcDbAttribute" in sub:
                        continue  # attributes are attached to the INSERT row instead
                    g = f.GetGeometryRef()
                    if g is None:
                        continue
                    try:
                        lg = g.GetLinearGeometry() or g
                    except Exception:
                        lg = g
                    sgeom = _wkb.loads(bytes(lg.ExportToWkb()))
                    if sgeom is None or sgeom.is_empty:
                        continue
                    agg = _collapse_gc_preserve_multis(sgeom, tol, say)
                    if not agg:
                        continue

                    attrs = {}
                    for name in ("EntityHandle", "Text", "Linetype"):
                        if name in field_names:
                            v = f.GetField(name)
                            if v not in (None, ""):
                                attrs[name] = v
                    h = attrs.get("EntityHandle")
                    if h and str(h) in inserts:
                        attrs.update(inserts[str(h)])

                    for gcode in ("LINE", "POLYGON", "POINT"):
                        geom_out = agg.get(gcode)
                        if geom_out is None or geom_out.is_empty:
                            continue
                        rows.append({"layer": layer_name, "geom": gcode, "geometry": geom_out, **attrs})
                        kept += 1
                except Exception as ex:
                    say(f"[ogr:warn] feature read failed: {ex}")
            say(f"[ogr] features total={total}, rows={kept}")
            ds = None

    if not rows:
        say("[ogr] no rows")
        return {}

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=f"EPSG:{source_epsg or 4326}")
    return _finalize(gdf, source_epsg, target_epsg, bbox_wgs84, say, tag="ogr")


# ---------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------

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
    merge_tolerance: float = 0.2,
    block_mode: str = "keep-merge",
    include_3d: bool = False,
    bbox_wgs84: Optional[Tuple[float, float, float, float]] = None,
    flat_dist_precise: float = 0.2,
    on_progress: ProgressCB = None,
    **kwargs
):
    """UI-facing wrapper. ezdxf available -> ezdxf pipeline; otherwise -> OGR fallback.

    `line_merge_tol=` is accepted as an alias of `merge_tolerance=`. All callers
    (standalone UI, Processing algorithm, batch tool) passed `line_merge_tol`,
    which used to fall into **kwargs and be silently ignored (always 0.2).
    """
    if "line_merge_tol" in kwargs and kwargs["line_merge_tol"] is not None:
        merge_tolerance = kwargs.pop("line_merge_tol")

    dxf_paths = _coerce_paths(src_path, kwargs.get("dxf_paths"))
    if not dxf_paths:
        raise TypeError("precise_convert: missing 'src_path' or 'dxf_paths'.")

    lyr_list = target_layers if target_layers is not None else layers
    if lyr_list is not None and not isinstance(lyr_list, (list, tuple, set)):
        lyr_list = [str(lyr_list)]

    s_epsg = src_epsg if src_epsg is not None else source_epsg
    t_epsg = tgt_epsg if tgt_epsg is not None else target_epsg
    s_epsg = int(s_epsg) if s_epsg else 3826
    t_epsg = int(t_epsg) if t_epsg else None
    tol = float(merge_tolerance or 0.0)
    flat = float(flat_dist_precise or 0.2)

    try:
        import ezdxf  # noqa: F401
        ez_ok = True
    except Exception:
        ez_ok = False

    if not ez_ok:
        _make_say(on_progress)("ezdxf not available → GDAL/OGR pipeline.")
        return _convert_ogr_fallback(
            dxf_paths=dxf_paths, source_epsg=s_epsg, target_epsg=t_epsg,
            target_layers=list(lyr_list) if lyr_list else None, bbox_wgs84=bbox_wgs84,
            line_merge_tol=tol, flat_dist_precise=flat, block_mode=block_mode,
            on_progress=on_progress,
        )

    return _precise_convert_ezdxf(
        dxf_paths=dxf_paths, source_epsg=s_epsg, target_epsg=t_epsg,
        include_3d=include_3d, flat_dist_precise=flat,
        target_layers=list(lyr_list) if lyr_list else None, bbox_wgs84=bbox_wgs84,
        on_progress=on_progress, block_mode=block_mode or "keep-merge", line_merge_tol=tol,
    )


# ---------------------------------------------------------------------
# Writers (GPKG / SHP / fallbacks)
# ---------------------------------------------------------------------

def _normalize_bucket_geoms(layer_geom_key: tuple, gdf):
    """Ensure geoms match the bucket type; drop incompatible/empty ones and all-null columns."""
    from shapely.geometry import Point, Polygon, MultiPoint, MultiPolygon
    want = (layer_geom_key[1] or "").upper()
    if want == "LINE":
        gdf = gdf.copy()
        gdf["geometry"] = [_extract_lineal(g) for g in gdf.geometry]
        gdf = gdf[gdf.geometry.notna()]
        gdf = gdf[~gdf.geometry.is_empty]
    elif want == "POLYGON":
        gdf = gdf.loc[gdf.geometry.apply(
            lambda g: isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty)].copy()
    elif want == "POINT":
        gdf = gdf.loc[gdf.geometry.apply(
            lambda g: isinstance(g, (Point, MultiPoint)) and not g.is_empty)].copy()
    # columns that only exist for other rows (e.g. ATT_* on plain lines) are all-null here
    keep_cols = [c for c in gdf.columns
                 if c in ("geometry", "layer", "geom") or gdf[c].notna().any()]
    return gdf[keep_cols]


def _jsonable(v):
    try:
        import numpy as np
        if isinstance(v, np.generic):
            v = v.item()
    except Exception:
        pass
    if v is None:
        return None
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(v, (int, bool, str)):
        return v
    return str(v)


def _write_prj(shp_path: str, crs):
    try:
        if crs is not None:
            with open(os.path.splitext(shp_path)[0] + ".prj", "w", encoding="utf-8") as f:
                f.write(crs.to_wkt("WKT1_ESRI"))
    except Exception:
        pass


def write_outputs(
    buckets: Dict[Tuple[str, str], "gpd.GeoDataFrame"],
    out_path: str,
    driver: str,
    overwrite: bool = False,
    on_progress: ProgressCB = None,
    layer_prefix: str = "",
):
    """Write buckets to:
      - GPKG: one file, one layer per bucket named "<prefix><Layer>_<GEOM>"
      - ESRI Shapefile: "<prefix><Layer>_<GEOM>.shp" per bucket
      - fallbacks (per failed bucket only): pyshp, then GeoJSON

    Returned dicts: {"path", "layer" (= GPKG layer / file stem actually written),
                     "src_layer", "geom", "count"}

    Fixes: GPKG layers were named only by CAD layer, so LINE/POLYGON/POINT of the
    same CAD layer overwrote each other; non-ASCII layer names collided in SHP.
    """
    from pathlib import Path
    say = _make_say(on_progress)
    written: List[dict] = []

    if not buckets:
        say("[write] empty buckets")
        return written

    used: set = set()
    names = {}
    for (layer, geom) in buckets.keys():
        names[(layer, geom)] = _unique_name(_sanitize_filename(f"{layer_prefix}{layer}_{geom}"), used)

    # ---------------- GPKG ----------------
    is_gpkg = (driver and driver.upper() == "GPKG") or (out_path and out_path.lower().endswith(".gpkg"))
    if is_gpkg:
        gpkg = out_path if out_path.lower().endswith(".gpkg") else os.path.join(out_path, "bundle.gpkg")
        os.makedirs(os.path.dirname(os.path.abspath(gpkg)), exist_ok=True)
        if overwrite and os.path.exists(gpkg):
            try:
                os.remove(gpkg)
            except Exception as ex:
                say(f"[write:warn] cannot remove existing {gpkg}: {ex}")
        for key, gdf in buckets.items():
            layer, geom = key
            lname = names[key]
            try:
                gdf = _normalize_bucket_geoms(key, gdf)
                if gdf.empty:
                    say(f"[write:skip] {layer}/{geom} empty after normalize")
                    continue
                gdf = _sanitize_fields_for_driver(gdf, "GPKG")
                gdf.to_file(gpkg, layer=lname, driver="GPKG")
                written.append({"path": gpkg, "layer": lname, "src_layer": layer, "geom": geom, "count": int(len(gdf))})
                say(f"[write] GPKG: {lname} ({len(gdf)}) → {gpkg}")
            except Exception as ex:
                say(f"[write:error] GPKG layer {lname} failed: {ex}")
        return written

    # ---------------- Shapefile ----------------
    os.makedirs(out_path, exist_ok=True)
    failed: Dict[Tuple[str, str], object] = {}
    for key, gdf in buckets.items():
        layer, geom = key
        stem = names[key]
        fpath = str(Path(out_path) / f"{stem}.shp")
        if overwrite:
            for ext in (".shp", ".shx", ".dbf", ".cpg", ".prj", ".qpj"):
                try:
                    os.remove(os.path.splitext(fpath)[0] + ext)
                except Exception:
                    pass
        try:
            gdf = _normalize_bucket_geoms(key, gdf)
            if gdf.empty:
                say(f"[write:skip] {layer}/{geom} empty after normalize")
                continue
            gdf.to_file(fpath, driver="ESRI Shapefile", encoding="UTF-8")
            written.append({"path": fpath, "layer": stem, "src_layer": layer, "geom": geom, "count": int(len(gdf))})
            say(f"[write] SHP: {stem} ({len(gdf)}) → {fpath}")
        except Exception as ex:
            failed[key] = gdf
            say(f"[write:warn] SHP writer failed, will try pyshp/GeoJSON: {fpath} → {ex}")

    if not failed:
        return written

    # ---------------- pyshp fallback (failed buckets only) ----------------
    still_failed: Dict[Tuple[str, str], object] = {}
    try:
        import shapefile as pyshp
        from shapely.geometry import mapping as shp_mapping
        say("[write] using pyshp fallback")

        def _shape_type(gt: str):
            u = (gt or "").upper()
            return {"POINT": pyshp.POINT, "LINE": pyshp.POLYLINE, "POLYGON": pyshp.POLYGON}.get(u, pyshp.NULL)

        for key, gdf in failed.items():
            layer, geom = key
            try:
                shp_type = _shape_type(geom)
                if shp_type == pyshp.NULL:
                    say(f"[write:warn] unsupported geom: {geom}")
                    continue
                fpath = str(Path(out_path) / f"{names[key]}_pyshp.shp")
                cols = [c for c in gdf.columns if c != "geometry"]
                used_f, fields = set(), []
                with pyshp.Writer(fpath, shp_type, encoding="utf-8") as w:
                    w.field("FID", "N", 18, 0)
                    used_f.add("fid")
                    num_cols = set(gdf.select_dtypes(include=["number"]).columns)
                    for c in cols:
                        fn = _unique_name(_sanitize_filename(c)[:8], used_f)[:10]
                        fields.append(c)
                        if c in num_cols:
                            w.field(fn, "F", 19, 6)
                        else:
                            w.field(fn, "C", 254)
                    for i in range(len(gdf)):
                        g = shp_mapping(gdf.geometry.iloc[i])
                        if shp_type == pyshp.POINT:
                            c0 = g["coordinates"][0] if g["type"] == "MultiPoint" else g["coordinates"]
                            w.point(c0[0], c0[1])
                        elif shp_type == pyshp.POLYLINE:
                            parts = [list(ls) for ls in g["coordinates"]] if g["type"] == "MultiLineString" else [list(g["coordinates"])]
                            w.line(parts)
                        else:
                            if g["type"] == "MultiPolygon":
                                parts = [list(r) for poly in g["coordinates"] for r in poly]
                            else:
                                parts = [list(r) for r in g["coordinates"]]
                            w.poly(parts)
                        rec = [i]
                        for c in fields:
                            v = _jsonable(gdf[c].iloc[i])
                            rec.append(("" if c not in num_cols else None) if v is None
                                       else (v if c in num_cols else str(v)[:254]))
                        w.record(*rec)
                _write_prj(fpath, gdf.crs)
                written.append({"path": fpath, "layer": names[key], "src_layer": layer, "geom": geom, "count": int(len(gdf))})
                say(f"[write] pyshp: {names[key]} ({len(gdf)}) → {fpath}")
            except Exception as ex:
                say(f"[write:warn] pyshp failed for {layer}/{geom}: {ex}")
                still_failed[key] = gdf
    except Exception as ex:
        say(f"[write:warn] pyshp unavailable: {ex}")
        still_failed = failed

    # ---------------- GeoJSON fallback ----------------
    if still_failed:
        import json
        from shapely.geometry import mapping as shp_mapping2
        say("[write] using GeoJSON fallback")
        for key, gdf in still_failed.items():
            layer, geom = key
            fpath = str(Path(out_path) / f"{names[key]}.geojson")
            try:
                g4326 = gdf.to_crs(4326) if gdf.crs is not None else gdf  # RFC 7946
                feats = []
                for i in range(len(g4326)):
                    props = {k: _jsonable(g4326[k].iloc[i]) for k in g4326.columns if k != "geometry"}
                    feats.append({"type": "Feature", "properties": props,
                                  "geometry": shp_mapping2(g4326.geometry.iloc[i])})
                with open(fpath, "w", encoding="utf-8") as f:
                    json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False)
                written.append({"path": fpath, "layer": names[key], "src_layer": layer, "geom": geom, "count": len(feats)})
                say(f"[write] GeoJSON: {names[key]} ({len(feats)}) → {fpath}")
            except Exception as ex:
                say(f"[write:error] GeoJSON failed: {fpath} → {ex}")
    return written
