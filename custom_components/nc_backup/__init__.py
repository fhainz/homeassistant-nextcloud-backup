"""nc_backup -- gehaerteter Nextcloud-WebDAV-Backup-Agent.

Kein eigenes Entity-Platform-Forwarding noetig: die `backup`-Kernkomponente
entdeckt jede Integration mit einem `backup.py`-Modul automatisch (derselbe
Mechanismus wie bei `diagnostics.py`) und ruft dort
`async_get_backup_agents`/`async_register_backup_agents_listener` auf --
siehe backup.py. Dieses Modul kuemmert sich nur um Entry-Setup/-Unload:
Session + Client aufbauen, Preflight-Check, Listener bei Aenderungen
benachrichtigen.
"""
from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .backup import notify_backup_listeners
from .client import NcAuthError, NcBackupClientError, NcWebDavClient
from .const import (
    CONF_ATTEMPTS,
    CONF_BACKUP_PATH,
    CONF_CHUNK_MB,
    CONF_PASSWORD,
    CONF_REQUEST_TOTAL,
    CONF_STALL_TIMEOUT,
    CONF_URL,
    CONF_USE_CHUNKED,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_ATTEMPTS,
    DEFAULT_CHUNK_MB,
    DEFAULT_REQUEST_TOTAL_S,
    DEFAULT_STALL_TIMEOUT_S,
    DEFAULT_USE_CHUNKED,
    DEFAULT_VERIFY_SSL,
)

_LOGGER = logging.getLogger(__name__)

# Plain-Assignment-Alias statt PEP-695-`type`-Statement (Python 3.12+) --
# so bleibt die Datei auch mit aelteren Python-Versionen syntaktisch pruefbar.
NcBackupConfigEntry = ConfigEntry[NcWebDavClient]


def _build_client(hass: HomeAssistant, entry: NcBackupConfigEntry) -> NcWebDavClient:
    # Eigene Session statt der geteilten HA-Session: mehrere parallele
    # Multi-GB-Streams (Backup-Upload + normaler HA-Traffic) sollen nicht um
    # dasselbe Connector-Limit konkurrieren -- das wuerde Wartezeit erzeugen,
    # die aiohttp auf das (kurze) connect-Budget anrechnet.
    verify_ssl = entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
    session = async_create_clientsession(
        hass, verify_ssl=verify_ssl, auto_cleanup=True, timeout=aiohttp.ClientTimeout(total=None)
    )
    options = entry.options
    return NcWebDavClient(
        session=session,
        base_url=entry.data[CONF_URL],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        backup_path=entry.data.get(CONF_BACKUP_PATH, "/Backups/HomeAssistant"),
        verify_ssl=verify_ssl,
        use_chunked=options.get(CONF_USE_CHUNKED, DEFAULT_USE_CHUNKED),
        chunk_mb=options.get(CONF_CHUNK_MB, DEFAULT_CHUNK_MB),
        stall_timeout_s=options.get(CONF_STALL_TIMEOUT, DEFAULT_STALL_TIMEOUT_S),
        attempts=options.get(CONF_ATTEMPTS, DEFAULT_ATTEMPTS),
        request_total_s=options.get(CONF_REQUEST_TOTAL, DEFAULT_REQUEST_TOTAL_S),
    )


async def async_setup_entry(hass: HomeAssistant, entry: NcBackupConfigEntry) -> bool:
    client = _build_client(hass, entry)
    try:
        if not await client.check_path(entry.data[CONF_BACKUP_PATH]):
            raise ConfigEntryNotReady(  # noqa: TRY301
                f"Backup-Pfad {entry.data[CONF_BACKUP_PATH]} nicht erreichbar/anlegbar"
            )
    except NcAuthError as err:
        raise ConfigEntryError(f"Zugangsdaten ungueltig: {err}") from err
    except (TimeoutError, aiohttp.ClientError, NcBackupClientError) as err:
        raise ConfigEntryNotReady(f"Nextcloud nicht erreichbar: {err}") from err

    entry.runtime_data = client
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    notify_backup_listeners(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NcBackupConfigEntry) -> bool:
    notify_backup_listeners(hass)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: NcBackupConfigEntry) -> None:
    """Nach einer Options-Aenderung (Timeouts/Chunkgroesse/Attempts) neu
    laden -- kein Core-Neustart noetig, nur fuer Code-Aenderungen."""
    await hass.config_entries.async_reload(entry.entry_id)
