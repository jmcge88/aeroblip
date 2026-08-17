"""Shared AeroDataBox request plumbing for both marketplaces."""
from __future__ import annotations


def request_parts(market: str, api_key: str, path: str) -> tuple[str, dict]:
    """Return (url, headers) for an AeroDataBox API path like "/flights/...\"."""
    if market == "apimarket":
        base = "https://prod.api.market/api/v1/aedbx/aerodatabox"
        headers = {"x-api-market-key": api_key}
    else:  # rapidapi
        base = "https://aerodatabox.p.rapidapi.com"
        headers = {
            "X-RapidAPI-Key": api_key,
            "X-RapidAPI-Host": "aerodatabox.p.rapidapi.com",
        }
    return base + path, headers
