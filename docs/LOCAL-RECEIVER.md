# Local receiver support (parked)

**Status: designed, not built.** Parked until someone on the project actually
runs an RTL-SDR receiver to test against — the feature is easy to get
approximately right and easy to get subtly wrong without real hardware.

## The idea

Everything in this app is throttled around adsb.lol's ~1 request / 10 s
rate limit — that's why dead reckoning exists. But readsb / dump1090 /
tar1090 all expose `aircraft.json` on the LAN with no rate limit at all. A
`LocalReceiverProvider` alongside the aggregators in
[`app/providers/radar.py`](../app/providers/radar.py) would let the poller run
at 1 s intervals with sub-second-fresh positions, falling back to the
aggregators for traffic beyond the receiver's horizon.

## Sketch

- New provider name `local` in `PROVIDERS`, URL from `LOCAL_RECEIVER_URL`
  (e.g. `http://192.168.1.20:8080/data/aircraft.json`).
- `aircraft.json` is already ADSBexchange-v2-shaped (`hex`, `flight`, `lat`,
  `lon`, `alt_baro`, `gs`, `track`, `seen_pos`…), so `_normalize` in the
  poller works as-is; the provider only needs to filter by distance from the
  poll centre (readsb reports everything it hears) and skip `seen_pos > 60`.
- No throttle entry (`_MIN_SPACING["local"] = 0`), `POLL_SECONDS` can drop to
  1–2 s when the active provider is local.
- Sticky-fallback already handles "receiver down → aggregator" — the local
  provider just becomes the preferred source.
- Personal mode only at first; product mode keeps adsb.lol.

If you build this: please also feed your receiver to adsb.lol
(<https://adsb.lol/feed/>) — this project leans on their community data.
