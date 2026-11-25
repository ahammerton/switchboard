# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Switchboard is a self-hosted home-lab start page: a Python server (standard library only, 3.9+) plus a vanilla HTML/JS front end. There is no build step, package manager, or linter, and having no dependencies is a headline feature of the project.

## Commands

Run from the repository root. On Windows use `python`; the README and Linux use `python3`.

```powershell
python server.py                                        # http://localhost:3000, creates ./config on first start
python -m unittest discover -s tests -t .               # full suite, as CI runs it (about 70 s)
python -m unittest tests.test_widgets                   # one module
python -m unittest tests.test_widgets -k sonarr         # by name
python -m unittest tests.test_widgets.ArrTests.test_sonarr   # one test
node --check public/app.js; node --check public/sw.js   # front-end syntax (also run in CI)
docker build --target test .                            # suite inside the image's base Python
```

Environment variables: `PORT`, `HOST`, `CONFIG_DIR`, `EDIT_TOKEN` (`off` disables it), `ALLOWED_HOSTS`, `HOST_NAME`. For manual testing, set `CONFIG_DIR` to a throwaway folder so the real `config/` isn't touched, and `EDIT_TOKEN=off` to skip the token prompt.

On Windows, `FirstStartTests.test_config_and_token_are_created` fails: it asserts file mode `0600`, which Windows doesn't support. `sysstats.py` only supports Linux and macOS.

## Architecture

- `server.py`: HTTP routes (`Handler`), config and secrets storage, status checks, weather, RSS, the Upcoming calendar, and all the caches. It is the composition root: the other modules never import it.
- `widgets.py`: app integrations (`PRESETS`, `CALENDARS`) and the bin collection lookups.
- `monitor.py`: a background thread for uptime history and alerts. It gets server functions as injected hooks, set up in `server.main()`.
- `discover.py`: "Find services" network scan. Uses `widgets.py` for Proxmox guests; its `APPS` table maps page titles to apps and widget types.
- `sysstats.py`: host CPU, RAM, disk and temperature from `/proc`, `/sys` or macOS tools.
- `public/app.js`: the whole UI in one file.

### State

Everything lives in `CONFIG_DIR`: `config.json` (the whole dashboard, sent to the browser), `secrets.json`, `edit-token`, `history.json`, `backgrounds/`, and a cached council list. Writes go to a temp file followed by `os.replace`.

- **The browser owns the config.** `app.js` holds it as `cfg`, mutates it in place, and `save()` PUTs the entire object. The server only normalises and validates it.
- **Config migration happens twice:** `normalise_config` in `server.py` and `normaliseConfig` in `app.js`. Older configs must keep loading, so treat new sections as optional on both sides. `default-config.json` is only copied on first start.
- **Secrets are write-only from the browser**, keyed by service `id`. `GET /api/secrets/<id>` returns only the names of the keys that are set. In a PUT, `""` keeps a key and `null` removes it. Saving the config calls `prune_secrets`, which drops entries for services without a widget; ids starting with `_` (such as `_alerts`, the webhook) are kept.

### Widgets

A preset is `fetch(ctx) -> [field(label, value, fmt, warn)]`. Raise `WidgetError` for messages the user should see; `widgets.run` turns any exception into `{"error": ...}` so one app can't break other tiles. Use `ctx.json` / `ctx.fetch` rather than urllib directly (timeouts, size limit, TLS, error text). Option entries with `type: "secret"` are stored in `secrets.json`. The edit dialog is generated from `/api/widgets/presets`, so a new preset needs no front-end code.

`warn=True` both shows the value in red and triggers an alert.

To add a widget: add the function and `PRESETS` entry in `widgets.py`, a mock to `tests/mock_apps.py`, and a test to `tests/test_widgets.py`. If it can be recognised by Find services, add it to `APPS` in `discover.py`; if it has a calendar, add it to `CALENDARS`.

`monitor.format_value` is the Python twin of `formatStat` in `app.js` (number formatting for alerts and tiles), so a new format goes in both.

### Security model

These are deliberate and tested in `tests/test_server.py`; SECURITY.md lists what is in and out of scope.

- Every request goes through `guard()` (Host allow-list against DNS rebinding). Every POST, PUT and DELETE uses `guard(changes=True)` (cross-site checks) and then `authorised()` (edit token, rate limited). `read_json_body` requires a JSON content type as part of the CSRF defence.
- Read-only endpoints only contact URLs already in the config, or fixed public APIs (Open-Meteo, the council list), so they can't be used to probe arbitrary hosts. Anything taking an arbitrary address needs the edit token.
- Requests to lab services (status checks, widgets, RSS, discovery, non-Discord webhooks) skip TLS verification on purpose (self-signed certificates). Fixed public APIs and Discord are verified.
- In `app.js`, build DOM with `el()` (it creates text nodes) and pass links through `safeUrl()`; config, feed and widget content is untrusted.

### Front end

`render()` rebuilds the page from `cfg`, then re-applies the last polled data (`applyStatus`, `applyWidgets`, `applyHistory`). Upcoming and News are built-in "special" tabs appended after `cfg.tabs` by `allTabs()`, so check `specialAt(ti)` before indexing `cfg.tabs`. Dialogs are built with `openForm`. The service worker caches only `offline.html`.

## Tests

`unittest` only. `tests/helpers.py` provides `MockApp` (an HTTP server on a free port) and `Switchboard` (the real `server.py` in a subprocess with a temporary config folder). `mock_apps.py` copies each app's real API shape and authentication. `test_frontend.py` fails if `app.js` looks up an element id that isn't in `index.html`, or if `index.html` loads a remote file.

## Conventions

- Code must run on Python 3.9 (CI covers 3.9 to 3.13).
- User-facing text and docs use British English.
- A new top-level Python module must be added to the `COPY` line in the `Dockerfile`, which lists files by name.
- User-visible changes go under `[Unreleased]` in `CHANGELOG.md` (Keep a Changelog, SemVer), and features are documented in `README.md`. `docs/DOCKERHUB.md` is the shorter Docker Hub page. The version is `VERSION` in `server.py`.
- Pushing to `main` publishes the `latest` image, and a `vX.Y.Z` tag publishes that release, once tests pass. The Gitea repository is a mirror of GitHub; work goes to GitHub.
