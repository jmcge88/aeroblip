# Contributing

Thanks for wanting to help. Bug reports, fixes and hardware notes are all
welcome.

## Licence and the CLA

This project is [AGPL-3.0](LICENSE). Contributions are accepted under the same
licence, **plus** a Contributor Licence Agreement: you keep the copyright in
your work, and you grant the project's author the right to relicense it,
including under commercial terms.

Why: the author doesn't want people to profiteer off this.

That is only possible while one party can license the whole codebase. Once a
contribution lands that the author cannot relicense, that option closes
permanently for everyone — so the CLA exists to keep it open, not to take
anything away from you. Your contribution stays free software under the AGPL for
every user, forever.

A CLA bot will ask you to sign on your first pull request. If you would rather
not sign, that is completely fine — open an issue describing the fix instead, and
it can be implemented independently.

## Before you open a pull request

- **Keep the data sources honest.** Outside `DEMO_MODE`, this project never
  fabricates data: no traffic means "clear skies", no board key means an empty
  board. Please don't add plausible-looking placeholder data.
- **Be kind to the aggregators.** The ADS-B networks are volunteer-run and this
  project is a pure consumer of them. Don't raise polling rates, don't remove the
  backoff in `app/providers/radar.py`, and don't add a source whose terms don't
  allow the use. If you run this at any scale,
  [feed a receiver](https://adsb.lol/feed/).
- **Respect `PRODUCT_MODE`.** It exists to restrict data to commercially
  licensed sources. If you add a source, put it on the correct side of that line
  and record it in `docs/DATA-SOURCES.md` and `THIRD-PARTY.md`.
- **Never render upstream strings raw.** Everything from an upstream API reaches
  the DOM through `esc()` / `escUrl()` in `static/app.js`. Same rule in
  `static/admin.html`.
- **Check licences of new dependencies.** Anything GPL-incompatible cannot go in.

## Development

```sh
cp .env.example .env
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # Linux/macOS: .venv/bin/pip
DEMO_MODE=true .venv/Scripts/uvicorn app.main:app --port 8001
```

`DEMO_MODE=true` fabricates traffic and board data, so you need no API keys and
no live aircraft overhead. The footer gains **SIMULATE FLYOVER** and
**SIMULATE 7700** buttons to drive the spotlight and emergency views.

Firmware lives in `esp32/` and builds with PlatformIO
(`python -m platformio run -e amoled216`). See [docs/FLASHING.md](docs/FLASHING.md).

## Style

Match the surrounding code. Comments here explain *why* rather than *what* —
please keep that up, especially where behaviour looks odd but is deliberate
(there is a lot of that in the page-rotation state machine and the route
cross-referencing).

There is no test suite yet. If you are adding one, the pure functions are the
place to start: `haversine_nm`, `is_airline_callsign`, `normalise_callsign`,
`parse_location`, `in_quiet_hours`, `etaToOverhead`.
