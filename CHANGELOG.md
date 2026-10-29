# Changelog

All notable changes to Switchboard are listed here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **A test suite.** It covers all 29 widgets (against mock apps), the server's security checks, alerts, uptime history, Find services and system stats, plus a front-end check. It runs on Python 3.9 to 3.13 in GitHub Actions, and images are only published when it passes.

### Changed
- **Python 3.9 or newer is now required.** 3.8 is past end-of-life.

### Fixed
- **A saved widget API key can now be removed.** Leaving the field blank keeps the saved key, so there was no way to delete one short of removing the widget or the service. Saved keys now have a **Remove saved key** button.
- **Error responses from apps are now closed properly.** Before, an app answering with an error status left its connection open until the server cleaned it up.

## [0.1.0] - 2026-10-28

The first public release.

### Added

- **Dashboard:** service tiles in groups, with icons (dashboard-icons, emoji or image URLs), descriptions and a live online/offline dot. Status checks run on the server, so CORS and self-signed certificates aren't a problem.
- **Tabs** for grouping services onto their own pages. Switch with keys 1–9, or link to one with `#tab-name`.
- **In-browser editing:** add, edit, reorder and delete tabs, groups and services. Drag tiles between groups or onto a tab. There's also a raw JSON editor, and import/export of the config.
- **App widgets** showing live stats on tiles, for 29 apps:
  - **\*arr and downloads:** Sonarr, Radarr, Lidarr, Readarr, Prowlarr, Bazarr, Seerr / Overseerr / Jellyseerr, qBittorrent, SABnzbd, NZBGet
  - **Media and books:** Plex, Jellyfin, Emby, Tautulli, Komga, BookOrbit, Magarr
  - **Infrastructure:** Proxmox VE, Portainer, Dockge, UniFi Network, Pi-hole (v6 and v5), AdGuard Home, Technitium DNS, Gitea / Forgejo
  - **Home:** Home Assistant, Mealie, Immich
  - **Anything else:** a custom JSON API widget

  Each widget has a **Test connection** button and says where to find the app's API key. API keys are kept on the server in `secrets.json` and never sent to the browser.
- **Find services:** scans the machines your services run on, running Proxmox guests, or a subnet you choose, and recognises 50+ apps. You get a list to tick, with icons and widget types filled in. Only private addresses are scanned. Apps already on the dashboard are skipped, even when your link uses a reverse-proxy name.
- **Alerts** to Discord or any JSON webhook:
  - when a service goes down, and when it comes back
  - when a widget shows a warning
  - when the host's temperature, RAM or disk passes its threshold

  They run on the server, so they work with no browser open. A problem must show up in 2 checks in a row before alerting, and alerts that happen together are grouped into one message.
- **24-hour uptime bars** on checked tiles, kept across restarts. They can be turned off in Settings.
- **Upcoming tab:** one calendar combining Sonarr, Radarr, Lidarr, Readarr and Magarr, grouped by day, with Downloaded / Missing badges.
- **News tab:** an RSS and Atom reader, with sample feeds including GitHub release feeds.
- **System stats bar** for the host:
  - CPU usage and temperature
  - free RAM and disk space
  - load average and uptime

  Usage bars turn red past the warning thresholds.
- **Weather widget** with a 3-day forecast, from Open-Meteo (no API key needed). You choose the location by city name.
- **Two interface styles:** Normal, and Glass, a translucent look inspired by Apple's Liquid Glass. You can switch on each device with ✨ or **G**.
- **Customisation:**
  - title and theme (auto, light or dark)
  - accent colour and tile size
  - background image: upload your own or paste a URL
  - search engine
- **Search bar** that filters services across all tabs, or searches the web.
- **Docker images** for amd64 and arm64.

### Security

- **An edit token is required for every change.** If you don't set one, it's generated on first start and saved in `config/edit-token`. `EDIT_TOKEN=off` opts out. Repeated wrong tokens are rate-limited.
- **Changes are only accepted from the dashboard's own page.** The browser's `Origin` and `Sec-Fetch-Site` headers are checked, and request bodies must be JSON. This blocks cross-site request forgery.
- **Requests for unknown public host names are refused**, which blocks DNS rebinding. Add the names you use, such as reverse-proxy domains, to `ALLOWED_HOSTS`.

[Unreleased]: https://github.com/ahammerton/switchboard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ahammerton/switchboard/releases/tag/v0.1.0
