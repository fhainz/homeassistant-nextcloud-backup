# Changelog nc_backup

## 0.1.0 (2026-08-17)
- Erste Version: gehärteter WebDAV-Backup-Agent als Ersatz für HAs
  eingebaute `webdav`-Integration (unregelmäßiger Upload-Stillstand bei
  ~4,7–5,3 GB Backups zum Reseller-gehosteten Nextcloud, siehe
  `wiki/meta/ha-backup-nextcloud-webdav.md`).
- Stall-Watchdog, begrenztes Gesamt-Timeout, Retry mit Backoff, optional
  Nextcloud-Chunked-Upload-v2, PROPFIND-Größenverifikation.
- Config Flow mit Zugangsdaten-Übernahme aus bestehendem `webdav`-Entry,
  Preflight-Testupload, Options-Flow für Timeouts/Chunkgröße/Versuche.
