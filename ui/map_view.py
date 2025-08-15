# dxf2gis_gui/ui/map_view.py
from __future__ import annotations
import json
from typing import Dict

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    _WEBENGINE_OK = True
except Exception:
    QWebEngineView = None
    _WEBENGINE_OK = False


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="initial-scale=1, width=device-width, user-scalable=no"/>
<link
  rel="stylesheet"
  href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
  integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
  crossorigin=""
/>
<style>
html, body, #map { height:100%; width:100%; margin:0; padding:0; }
.leaflet-container { background:#f7f7f7; }
.layer-label {
  position: absolute;
  top: 8px; left: 8px;
  background: rgba(255,255,255,0.85);
  padding: 6px 8px; border-radius: 4px;
  font: 12px/1.2 Arial, sans-serif;
  max-width: 40%;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
</style>
</head>
<body>
<div id="map"></div>
<div id="ll" class="layer-label" style="display:none;"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
  integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
  crossorigin=""></script>
<script>
var map = L.map('map', {zoomControl:true}).setView([23.5, 121], 7);
var base = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap'
}).addTo(map);

var group = L.layerGroup().addTo(map);
var lbl = document.getElementById('ll');

function styleByGeom(geomType) {
  switch (geomType) {
    case 'Point': case 'MultiPoint':
      return null; // handled by pointToLayer
    case 'LineString': case 'MultiLineString':
      return {color:'#0077ff', weight:1.2, opacity:0.9};
    case 'Polygon': case 'MultiPolygon':
      return {color:'#ff6a00', weight:1, fillColor:'#ff9d57', fillOpacity:0.25};
    default:
      return {color:'#666', weight:1, opacity:0.8};
  }
}

function pointSymbol(feature, latlng) {
  return L.circleMarker(latlng, {radius:3, color:'#1f7a1f', fillColor:'#2ecc71', fillOpacity:0.8, weight:1});
}

window.showGeoJSON = function(payload) {
  group.clearLayers();
  var bounds = null;
  var labels = [];

  for (var lname in payload) {
    try {
      var gj = payload[lname];
      var layer = L.geoJSON(gj, {
        style: function(f) { return styleByGeom(f.geometry ? f.geometry.type : ''); },
        pointToLayer: pointSymbol
      });
      layer.addTo(group);
      labels.push(lname + " (" + layer.getLayers().length + ")");
      try {
        var b = layer.getBounds();
        if (b && b.isValid && b.isValid()) {
          bounds = bounds ? bounds.extend(b) : b;
        }
      } catch(e){}
    } catch(e) {
      console.error("Failed layer:", lname, e);
    }
  }

  if (bounds) {
    try { map.fitBounds(bounds.pad(0.1)); } catch(e) {}
  }

  if (labels.length) {
    lbl.style.display = 'block';
    lbl.textContent = labels.join(' | ');
  } else {
    lbl.style.display = 'none';
  }
};
</script>
</body>
</html>
"""


class MapView(QWidget):
    """Leaflet map inside QWebEngineView. Exposes `show_geojson(dict)` to render."""
    def __init__(self, parent=None):
        super().__init__(parent)
        lyt = QVBoxLayout(self)
        lyt.setContentsMargins(0, 0, 0, 0)

        if not _WEBENGINE_OK:
            lyt.addWidget(QLabel(
                "Qt WebEngine is not available.\n"
                "Please install PySide6-Addons (QtWebEngine) to enable map preview."
            ))
            self._view = None
            return

        self._view = QWebEngineView(self)
        lyt.addWidget(self._view, 1)
        self._loaded = False

    def load_empty(self):
        if not self._view:
            return
        # Set inline HTML (Leaflet via CDN)
        self._view.setHtml(_HTML, baseUrl=QUrl("https://local.resource/"))
        self._loaded = True

    def show_geojson(self, layer_to_geojson: Dict[str, dict]):
        """Render a dict: { 'LayerName (GEOM)': <GeoJSON dict> }."""
        if not self._view:
            return
        if not self._loaded:
            self.load_empty()

        # Serialize payload to JS
        try:
            js_arg = json.dumps(layer_to_geojson)
        except Exception:
            # As fallback, try to coerce each value to dict
            fixed = {}
            for k, v in layer_to_geojson.items():
                if isinstance(v, str):
                    try:
                        fixed[k] = json.loads(v)
                    except Exception:
                        continue
                else:
                    fixed[k] = v
            js_arg = json.dumps(fixed)

        self._view.page().runJavaScript(f"window.showGeoJSON({js_arg});")
