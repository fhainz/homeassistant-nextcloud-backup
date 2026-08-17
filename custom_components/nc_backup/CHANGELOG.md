# Changelog nc_backup

## 0.1.0 (2026-08-17)
- Initial release: hardened WebDAV backup agent replacing HA's built-in
  `webdav` integration (intermittent upload stalls at ~4.7–5.3 GB
  backups to reseller-hosted Nextcloud).
- Stall watchdog, capped total timeout, retry with backoff, optional
  Nextcloud Chunked Upload v2, PROPFIND size verification.
- Config flow with credential takeover from an existing `webdav` entry,
  preflight test upload, options flow for timeouts/chunk size/attempts.
