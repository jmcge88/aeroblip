# Data sources & integrations

**As of 16 Aug 2026** — after the standing-data migration
(`docs/RESEARCH-metadata-sources.md` §8) and the airplanes.live removal below.
Metadata no longer costs anything per lookup; AeroDataBox is the only paid
integration and serves the airport board alone.

## What we use for what

| Need | Product mode | Personal / self-hosted | Licence | Cost |
|---|---|---|---|---|
| **Aircraft positions** (overhead + nearby) | adsb.lol point query | adsb.lol / adsb.fi (sticky fallback) | ODbL — attribution shown on device & web footer | Free |
| **Airframe basics** (reg, type, description) | `r`/`t`/`desc` fields already in the position feed | same | ODbL (part of feed) | Free |
| **Routes** (callsign → origin/dest/airline) | VRS standing-data → local SQLite (`StandingDataMeta`) | adsbdb.com API (`AdsbdbMeta`) | CC0-1.0 / adsbdb unlicensed for commercial use | Free |
| **Airlines** (ICAO prefix → name/IATA) | standing-data SQLite | adsbdb.com | CC0-1.0 | Free |
| **Airframe extras** (manufacturer, model, operator) | standing-data SQLite (sparse; feed fields are primary) | adsbdb.com | CC0-1.0 | Free |
| **Aircraft photos** | none (no commercially licensed source) | adsbdb → planespotters.net URLs | Non-commercial | Free |
| **Airport board** (arrivals/departures FIDS) | AeroDataBox | AeroDataBox | Paid plan | **Only paid integration.** Scales per *airport* (~54 calls/airport/day at 20-min refresh with quiet hours), not per device |
| **Global 7700 watch** | adsb.lol squawk endpoint | adsb.lol only — **no fallback** | ODbL | Free |
| **7700 place names** (reverse geocode) | bigdatacloud.net client API | same | Free client tier | Free |
| **Airline logos** | logostream.dev (`LOGO_URL_TEMPLATE`), 30-day server cache | images.kiwi.com | logostream free tier (terms unconfirmed, see below) | Free tier |

### How the metadata path works (product mode)

- `app/providers/standing_data.py` downloads the
  [vradarserver/standing-data](https://github.com/vradarserver/standing-data)
  tarball (CC0), builds `DATA_DIR/standing_data.db` (~618k routes, ~34k
  airports, ~6k airlines, ~17k airframes; build takes seconds) and refreshes
  it daily (`STANDING_DATA_REFRESH_HOURS`).
- `CachedMeta` (disk-backed, fleet-wide) sits above it; the live airport
  board still corrects stale routes (`_apply_board_routes`) and implausible
  routes are suppressed (`_drop_implausible_routes`).
- First-ever boot blocks on the initial sync so caches don't fill with misses.

### Keys / env

| Env var | Used for | Required |
|---|---|---|
| `AERODATABOX_API_KEY` | Airport board only | Yes, for a board (empty board without it) |
| `LOGO_API_KEY` | logostream.dev (product mode logos) | Product mode only |
| `STANDING_DATA_URL` / `STANDING_DATA_REFRESH_HOURS` | Metadata sync | No (defaults fine) |
| Device tokens (`REQUIRE_DEVICE_TOKEN`, `ADMIN_TOKEN`) | Own fleet API, not an external integration | Product mode |
