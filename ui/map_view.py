# dxf2gis_gui/ui/map_view.py
from __future__ import annotations
import json
from typing import Dict, Any, Tuple, Optional, List

from PySide6.QtCore import QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView


_LEAFLET_HTML_TMPL = """<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DXF Preview Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
html, body, #map { width:100%; height:100%; margin:0; padding:0; }
.leaflet-tooltip.layer-label { background:rgba(0,0,0,0.6); color:#fff; border:none; }
</style>
</head>
<body>
<div id="map"></div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// Initialize map
const map = L.map('map', { preferCanvas: true }).setView([23.7, 121.0], 6);

// Base layer (no SRI / crossorigin to avoid local restrictions)
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '© OpenStreetMap contributors'
}).addTo(map);

// Global feature group
window._fg = L.featureGroup().addTo(map);

// Colors
const COLORS = [
  '#e41a1c','#377eb8','#4daf4a','#984ea3','#ff7f00',
  '#ffff33','#a65628','#f781bf','#999999'
];
function colorFor(i) { return COLORS[i % COLORS.length]; }

function clearLayers() {
  try { window._fg.clearLayers(); } catch(e) {}
}

function addGeoJson(name, gj, colorIdx) {
  const style = function(f) {
    return {
      color: colorFor(colorIdx),
      weight: 2,
      opacity: 0.9,
      fillColor: colorFor(colorIdx),
      fillOpacity: 0.2
    };
  };
  const layer = L.geoJSON(gj, {
    style: style,
    pointToLayer: (f, latlng) => L.circleMarker(latlng, {
      radius: 4, color: colorFor(colorIdx),
      fillColor: colorFor(colorIdx), fillOpacity: 0.9, weight: 1
    })
  });
  layer.bindTooltip(name, {className:'layer-label'});
  window._fg.addLayer(layer);
  return layer;
}

// Set layers from payload: { "Layer (GEOM)": FeatureCollection, ... }
window.setGeoJsonLayers = function(payload) {
  clearLayers();
  const names = Object.keys(payload || {});
  names.sort();
  names.forEach((name, idx) => {
    try { addGeoJson(name, payload[name], idx); }
    catch(e) { console.error('Failed to add layer', name, e); }
  });
};

window.fitToBounds = function(swLat, swLng, neLat, neLng) {
  try {
    const b = L.latLngBounds(L.latLng(swLat, swLng), L.latLng(neLat, neLng));
    if (b.isValid()) map.fitBounds(b, {padding:[20,20]});
  } catch(e) { console.error('fitToBounds error', e); }
};
</script>
</body>
</html>
"""

class MapView(QWebEngineView):
    """
    Leaflet-based preview in a QWebEngineView.
    expose:
      - load_empty()
      - show_geojson(dict[str -> GeoJSON])
      - fit_bounds((south, west, north, east))
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._ready: bool = False
        self._pending_js: List[str] = []
        self.loadFinished.connect(self._on_loaded)

    def _on_loaded(self, ok: bool):
        self._ready = bool(ok)
        if self._ready and self._pending_js:
            for code in self._pending_js:
                try:
                    self.page().runJavaScript(code)
                except Exception:
                    pass
            self._pending_js.clear()

    def load_empty(self):
        # Use setHtml to avoid local file issues and SRI restrictions
        self._ready = False
        # baseUrl helps Leaflet fetch relative assets if any (we use CDN anyway)
        self.setHtml(_LEAFLET_HTML_TMPL, baseUrl=QUrl("https://unpkg.com/"))

    def _run_js(self, code: str):
        try:
            if self._ready and self.page():
                self.page().runJavaScript(code)
            else:
                self._pending_js.append(code)
                # ensure page is loaded once
                if self.url().isEmpty():
                    self.load_empty()
        except Exception:
            pass

    def show_geojson(self, layers: Dict[str, Any]):
        if self.url().isEmpty():
            self.load_empty()
        payload = json.dumps(layers, ensure_ascii=False)
        self._run_js(f"window.setGeoJsonLayers({payload});")

    def fit_bounds(self, bounds: Tuple[float, float, float, float]):
        if not bounds:
            return
        s, w, n, e = bounds
        self._run_js(f"window.fitToBounds({s}, {w}, {n}, {e});")
