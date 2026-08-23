"""Aviation weather (METAR/TAF) from aviationweather.gov.

US government data: free, no API key, public domain, and global coverage -
the one weather source that costs nothing in any deployment mode. Cached by
the caller (main.py) for WX_TTL so a wall of dashboards costs one upstream
request per airport per ten minutes.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

METAR_URL = "https://aviationweather.gov/api/data/metar?ids={icao}&format=json"
TAF_URL = "https://aviationweather.gov/api/data/taf?ids={icao}&format=json"


def _fmt_clouds(clouds: list | None) -> str | None:
    """[{cover: 'FEW', base: 3000}] -> 'FEW030'; CAVOK/CLR pass through."""
    if not clouds:
        return None
    out = []
    for c in clouds:
        cover = c.get("cover") or ""
        base = c.get("base")
        out.append(f"{cover}{int(base) // 100:03d}" if isinstance(base, (int, float))
                   else cover)
    return " ".join(x for x in out if x) or None


class WxProvider:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def fetch(self, icao: str) -> dict | None:
        """Latest METAR (decoded basics + raw) and raw TAF for one station."""
        try:
            resp = await self._client.get(METAR_URL.format(icao=icao), timeout=15)
            resp.raise_for_status()
            reports = resp.json()
        except Exception as exc:
            log.warning("metar fetch failed for %s: %s", icao, exc)
            return None
        if not isinstance(reports, list) or not reports:
            return None
        m = reports[0]
        taf = None
        try:
            resp = await self._client.get(TAF_URL.format(icao=icao), timeout=15)
            resp.raise_for_status()
            tafs = resp.json()
            if isinstance(tafs, list) and tafs:
                taf = tafs[0].get("rawTAF")
        except Exception as exc:  # a missing TAF is not a missing METAR
            log.warning("taf fetch failed for %s: %s", icao, exc)

        def num(v):
            return v if isinstance(v, (int, float)) else None

        return {
            "icao": m.get("icaoId") or icao,
            "name": m.get("name"),
            "observed": num(m.get("obsTime")),
            "temp_c": num(m.get("temp")),
            "dewpoint_c": num(m.get("dewp")),
            # wdir is degrees or the string "VRB" (variable) - keep both forms
            "wind_dir": m.get("wdir") if m.get("wdir") == "VRB" else num(m.get("wdir")),
            "wind_kt": num(m.get("wspd")),
            "gust_kt": num(m.get("wgst")),
            "visibility_sm": m.get("visib"),  # statute miles; "10+" means unlimited
            "qnh_hpa": round(num(m.get("altim")) or 0) or None,
            "wx": m.get("wxString") or None,
            "clouds": _fmt_clouds(m.get("clouds")),
            "raw": m.get("rawOb"),
            "taf": taf,
        }
