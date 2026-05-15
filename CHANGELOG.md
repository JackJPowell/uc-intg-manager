# Integration manager Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

_Changes in the next release_

---

## v2.0.0 - 2025-05-15

### Changes

- The webserver port is changing. If you navigate to 8088 the remote will present a splash screen notifying you of the new port `9999` and will redirect you after 10 seconds. If you have any bookmarks or saved links, please update them after the redirect occurs.

### Added

- If you are running firmware 2.9.3 or later, all integrations can be updated in place! If you are running a version less than 2.9.3, functionality will remain for now but will be removed eventually.

---

## v1.7.4 - 2025-05-12

### Fixed
- In certain circumstances when adding a remote, a previously created api key would not be found and a failure would occur when registering a new one
- Additional remotes would sometimes be reported as offline incorrectly
- Asyncio error related to zeroconf has been squished

---

## v1.7.3 - 2026-05-03

### Fixed
- If you had multiple remotes configured in integration manager, in certain conditions, only the first remote would be reported as online

## Previous Release Notes

### Added
- **Sponsorship links** — Integration and available cards now show a heart button for developers who have set up sponsorship links. Hovering reveals platform options (GitHub Sponsors, Buy Me a Coffee, PayPal, Ko-fi, and more).
- **Unused activity entities diagnostic** — The diagnostics page now surfaces entities that are added to the activity but are not used anywhere inside the activity.
- **Offline Remote Improvements** - Remotes that are offline will no longer delay page loading PR/27 Thanks @henrikwidlund!
- **Developer homepage links** — The developer name on both installed and available integration cards is now a clickable link to the developer's homepage when one is provided in the registry.


### Changed
- **Diagnostics page layout** — Diagnostic sections are now collapsible. System Controls have been moved to the bottom of the page. Navigation buttons have correct colors in both light and dark mode.

### Fixed
- **Battery/dock restart** — When `shutdown_on_battery` is enabled and the remote is re-docked, the web server now correctly restarts. Previously, stale references left after undocking prevented the server from being created again.
- **Improved Version Detection** -- Previously, certain tags on a version would be misinterptred. PR/28 Thanks @henrikwidlund!
---

## v1.7.2 - 2026-05-02
### Added
- **Sponsorship links** — Integration and available cards now show a heart button for developers who have set up sponsorship links. Hovering reveals platform options (GitHub Sponsors, Buy Me a Coffee, PayPal, Ko-fi, and more).
- **Unused activity entities diagnostic** — The diagnostics page now surfaces entities that are added to the activity but are not used anywhere inside the activity.
- **Offline Remote Improvements** - Remotes that are offline will no longer delay page loading PR/27 Thanks @henrikwidlund!
- **Developer homepage links** — The developer name on both installed and available integration cards is now a clickable link to the developer's homepage when one is provided in the registry.


### Changed
- **Diagnostics page layout** — Diagnostic sections are now collapsible. System Controls have been moved to the bottom of the page. Navigation buttons have correct colors in both light and dark mode.

### Fixed
- **Battery/dock restart** — When `shutdown_on_battery` is enabled and the remote is re-docked, the web server now correctly restarts. Previously, stale references left after undocking prevented the server from being created again.
- **Improved Version Detection** -- Previously, certain tags on a version would be misinterptred. PR/28 Thanks @henrikwidlund!
---

## v1.6.7 - 2026-04-19
### Added
- **Self-update** — Integration Manager can now update itself directly from the web UI. Clicking it installs a temporary bootstrapper integration on the remote, which downloads the new IM release from GitHub, replaces the old installation, restores all settings and backups, then removes itself — no manual intervention required.
- **Firmware update check** — The diagnostics page now shows the current remote firmware version and highlights when a newer firmware release is available.

### Changed
- **Async web server** — Migrated from Flask to Quart (async-native). All route handlers are now `async`, enabling concurrent API calls without threading overhead.
- **Async API client** — `sync_api.py` rewritten to use `aiohttp` throughout, removing synchronous `requests` calls from route handlers.
- **Dependency updates** — `ucapi` bumped to `0.6.0`, `ucapi-framework` to `1.9.1`.

### Fixed
- **Docker backup support** — Integrations running in Docker containers can now be backed up correctly.
- **Entity reconfigure on update** — Fixed a bug where updating an integration would not restore your configured entities after an upgrade.
- 
---

## v1.6.6 - 2026-04-19
### Added
- **Self-update** — Integration Manager can now update itself directly from the web UI. Clicking it installs a temporary bootstrapper integration on the remote, which downloads the new IM release from GitHub, replaces the old installation, restores all settings and backups, then removes itself — no manual intervention required.
- **Firmware update check** — The diagnostics page now shows the current remote firmware version and highlights when a newer firmware release is available.

### Changed
- **Async web server** — Migrated from Flask to Quart (async-native). All route handlers are now `async`, enabling concurrent API calls without threading overhead.
- **Async API client** — `sync_api.py` rewritten to use `aiohttp` throughout, removing synchronous `requests` calls from route handlers.
- **Dependency updates** — `ucapi` bumped to `0.6.0`, `ucapi-framework` to `1.9.1`.

### Fixed
- **Docker backup support** — Integrations running in Docker containers can now be backed up correctly.
- **Entity reconfigure on update** — Fixed a bug where updating an integration would not restore your configured entities after an upgrade.

---

## v1.6.5 - 2026-04-18
### Fixed
- **Updating page poll delay** — The `/updating` page now waits 15 seconds before starting to poll `/health`, giving the bootstrapper time to fully uninstall the old Integration Manager before the page tries to reconnect.

---

## v1.6.4 - 2026-04-18
### Fixed
- **Release artifact name** — Build workflow now produces `uc-intg-manager-<version>-aarch64.tar.gz` (was `uc-intg-intg_manager_driver-...`), matching the bootstrapper's asset pattern so self-updates can find the correct file.
- **Upgrade overlay delay** — Upgrade overlay now appears immediately on click rather than waiting for the HTMX indicator debounce delay, both from the direct update button and the version selector modal.

---

## v1.6.3 - 2026-04-18
### Fixed
- **Version selector downgrade** — All "Select Version" buttons on the Integration Manager card now correctly route through the self-update bootstrapper flow (`/api/self-update`) instead of the standard integration install route.

---

## v1.6.2 - 2026-04-18
### Fixed
- **SSL certificate verification** — GitHub API calls from the async `GitHubClient` now use a certifi-backed SSL context, resolving `CERTIFICATE_VERIFY_FAILED` errors when checking for updates on the remote.

---

## v1.6.1 - 2026-04-18
### Added
- **Self-update** — Integration Manager can now update itself directly from the web UI. Clicking it installs a temporary bootstrapper integration on the remote, which downloads the new IM release from GitHub, replaces the old installation, restores all settings and backups, then removes itself — no manual intervention required.
- **Firmware update check** — The diagnostics page now shows the current remote firmware version and highlights when a newer firmware release is available.

### Changed
- **Async web server** — Migrated from Flask to Quart (async-native). All route handlers are now `async`, enabling concurrent API calls without threading overhead.
- **Async API client** — `sync_api.py` rewritten to use `aiohttp` throughout, removing synchronous `requests` calls from route handlers.
- **Dependency updates** — `ucapi` bumped to `0.6.0`, `ucapi-framework` to `1.9.1`.

### Fixed
- **Docker backup support** — Integrations running in Docker containers can now be backed up correctly.
- **Entity reconfigure on update** — Fixed a bug where updating an integration would not restore your configured entities after an upgrade.

---

## v1.5.3 - 2026-03-12
### Added
- **Backup & restore for integrations** — One-click backup and restore of integration configurations, stored in `manager.json`. Backups survive Integration Manager updates and reinstalls.
- **Orphaned entity cleanup** — Detects and removes entity assignments left behind when an integration is deleted from the remote.

---

## v1.5.2 - 2026-02-23
### Added
- **Remote section in sidebar** — Quick-access links to the active remote's Web Configurator and Core REST API documentation
- **Integration log multi-select** — The service filter on the Integration Logs page now supports selecting multiple integrations simultaneously. Logs are merged and sorted newest-first. Download includes all selected services in a single file.
- **Styled log level picker** — The Log Level filter now uses the same custom dropdown style as the service selector, replacing the native browser select element.

### Fixed
- **Backup reliability** — Integration backups would intermittently fail because the manager polled for backup data before the integration had finished connecting to its device. The single fixed-delay GET is now replaced with a polling loop that waits up to 15 seconds for the integration to signal it is ready, eliminating the race condition.

---

## v1.0.2 - 2025-12-12
### Corrections
- More Pipeline fixes

## v1.0.1 - 2025-12-12
### Corrections
- Pipeline fixes

## v1.0.0 - 2025-12-12
