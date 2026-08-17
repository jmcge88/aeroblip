# Vendored third-party assets

Served locally rather than hotlinked from a CDN. A CDN compromise would
otherwise get script execution on every dashboard, and a LAN kiosk should not
need internet access to render its own map chrome.

## Leaflet 1.9.4

- Upstream: <https://leafletjs.com> — BSD-2-Clause, (c) 2010-2023 Volodymyr
  Agafonkin, (c) 2010-2011 CloudMade. Licence text is preserved in the
  `@preserve` header of `leaflet.js`.
- Fetched from `https://unpkg.com/leaflet@1.9.4/dist/`.
- Verified against the published subresource-integrity digests:

  ```text
  leaflet.js   sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=
  leaflet.css  sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=
  ```

  Re-check after any update:

  ```bash
  python -c "import hashlib,base64,sys;print('sha256-'+base64.b64encode(hashlib.sha256(open(sys.argv[1],'rb').read()).digest()).decode())" static/vendor/leaflet.js
  ```

`images/` holds the five sprites `leaflet.css` references. The dashboard uses
`divIcon`/`circleMarker` with map controls disabled, so none are currently
loaded; they are kept so the stylesheet is self-contained.

Map *tiles* still come from `basemaps.cartocdn.com` at runtime. That is data
rather than executable code, so it is not vendored — but it does mean the
emergency map needs internet access, and the tile host sees the dashboard's
requests.
