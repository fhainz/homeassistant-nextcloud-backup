"""Konstanten fuer die nc_backup-Integration.

Grund fuer die Existenz dieser Integration: HAs eingebaute `webdav`-
Backup-Integration haengt beim Hochladen des ~4,7-5,3 GB grossen
Home-Assistant-Backups zum Reseller-gehosteten Nextcloud unregelmaessig
fest und stirbt erst nach dem vollen `total=43200`-Timeout (12h) --
kein zu kurzer Timeout, sondern ein echter Stillstand ohne Fortschritt
(z.B. Proxy des Hosters puffert den kompletten Body). `webdav` hat
zudem keinen Options-Flow, ist also nicht nachtraeglich justierbar.
Diese Integration ersetzt sie: Stall-Watchdog auf Byte-Fortschritt
(nicht nur ein globaler Timeout), begrenztes Gesamt-Timeout, Retry mit
Backoff, optional Nextcloud-Chunked-Upload-v2 (jeder Chunk einzeln
retrybar), PROPFIND-Groessenverifikation nach dem Upload.

Siehe wiki/meta/ha-backup-nextcloud-webdav.md fuer den Hintergrund.
"""
from __future__ import annotations

DOMAIN = "nc_backup"

# hass.data-Schluessel fuer die Liste der Backup-Agent-Listener (siehe backup.py).
DATA_LISTENERS = f"{DOMAIN}_listeners"

# Cache-Dauer der Backup-Liste (PROPFIND + n Metadaten-GETs sind nicht gratis).
CACHE_TTL_S = 300

# --- Config-Entry-Datenfelder (Zugangsdaten) ---
CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_BACKUP_PATH = "backup_path"
CONF_VERIFY_SSL = "verify_ssl"

# --- Options-Felder (Tuning, nachtraeglich per OptionsFlow aenderbar) ---
CONF_USE_CHUNKED = "use_chunked"
CONF_CHUNK_MB = "chunk_mb"
CONF_STALL_TIMEOUT = "stall_timeout"
CONF_ATTEMPTS = "attempts"
CONF_REQUEST_TOTAL = "request_total"

DEFAULT_BACKUP_PATH = "/Backups/HomeAssistant"
DEFAULT_VERIFY_SSL = True
DEFAULT_USE_CHUNKED = True
DEFAULT_CHUNK_MB = 64
DEFAULT_STALL_TIMEOUT_S = 300
DEFAULT_ATTEMPTS = 4
DEFAULT_REQUEST_TOTAL_S = 7200

# Feste (nicht ueber UI konfigurierbare) Timeout-Bausteine.
DEFAULT_CONNECT_S = 30
DEFAULT_CHUNK_TOTAL_S = 900
DEFAULT_CTRL_TOTAL_S = 120
DEFAULT_MOVE_TOTAL_S = 1800

# Backoff-Stufen zwischen Retries (Sekunden), letzte Stufe wiederholt sich.
BACKOFF_STEPS_S = (30, 120, 300)

# Nextcloud-Chunked-Upload-v2-Grenzen (serverseitig fest, nicht unsere Wahl).
NC_MIN_CHUNK_BYTES = 5 * 1024 * 1024
NC_MAX_CHUNKS = 10000

# HTTP-Status-Codes, bei denen ein Retry NICHT sinnvoll ist (Auth/Pfad/Quota).
FATAL_STATUS_CODES = frozenset({401, 403, 404, 405, 409, 507})
