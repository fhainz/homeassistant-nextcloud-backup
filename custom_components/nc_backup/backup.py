"""Backup-Agent-Plattform fuer nc_backup.

Implementiert HAs `BackupAgent`-Kontrakt (homeassistant.components.backup,
seit Core 2024.12) gegen den gehaerteten Client aus client.py. Dateinamen
werden 1:1 wie bei der eingebauten `webdav`-Integration gebildet
(suggested_filename) -- dadurch sieht dieser Agent auch Backups, die der
alte webdav-Agent schon hochgeladen hat (Kompatibilitaet/Rollback-Pfad),
und umgekehrt.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from functools import wraps
from time import time as wall_time
from typing import Any

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
    suggested_filename,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .client import (
    RETRYABLE_EXC,
    NcAuthError,
    NcNotFoundError,
    NcStalledError,
    backoff_delay,
)
from .const import CACHE_TTL_S, CONF_BACKUP_PATH, DATA_LISTENERS, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _handle_errors(func: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Konvertiert JEDE Ausnahme in eine `BackupAgentError`/`BackupNotFound`.

    Wichtig: der HA-Backup-Manager loggt alles, was NICHT eine
    BackupAgentError ist, als "Unexpected error" mit vollem Traceback und
    wirkt dadurch wie ein Integrationsbug statt einem erwarteten
    Netzwerkfehler -- siehe Plan Abschnitt 8.5.
    """

    @wraps(func)
    async def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except (BackupAgentError, BackupNotFound):
            raise
        except NcNotFoundError as err:
            raise BackupNotFound(str(err)) from err
        except NcAuthError as err:
            raise BackupAgentError(f"Zugangsdaten abgelehnt: {err}") from err
        except Exception as err:  # noqa: BLE001 -- bewusst alles einfangen, s.o.
            raise BackupAgentError(str(err)) from err

    return _wrapped


def _suggested_filenames(backup: AgentBackup) -> tuple[str, str]:
    """tar-/metadata.json-Namen exakt wie die Core-`webdav`-Integration."""
    base = suggested_filename(backup)
    if base.endswith(".tar"):
        base = base[: -len(".tar")]
    return f"{base}.tar", f"{base}.metadata.json"


async def async_get_backup_agents(hass: HomeAssistant, **kwargs: Any) -> list[BackupAgent]:
    """Von HAs Backup-Manager beim Aufbau der Ziel-Liste aufgerufen."""
    return [
        NcBackupAgent(hass, entry)
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant, *, listener: Callable[[], None], **kwargs: Any
) -> Callable[[], None]:
    """Registriert einen Listener, der bei Agent-Zu-/Abgang (Entry
    hinzugefuegt/entfernt/neu geladen) benachrichtigt werden will."""
    listeners: list[Callable[[], None]] = hass.data.setdefault(DATA_LISTENERS, [])
    listeners.append(listener)

    @callback
    def _remove() -> None:
        if listener in listeners:
            listeners.remove(listener)

    return _remove


def notify_backup_listeners(hass: HomeAssistant) -> None:
    """Von __init__.py nach Setup/Unload/Reload eines Entry aufgerufen."""
    for listener in list(hass.data.get(DATA_LISTENERS, [])):
        listener()


class NcBackupAgent(BackupAgent):
    """Ein Config Entry == ein Backup-Ziel/Agent."""

    domain = DOMAIN

    def __init__(self, hass: HomeAssistant, entry: Any) -> None:
        self._hass = hass
        self._entry = entry
        self._client = entry.runtime_data
        self.name = entry.title
        self.unique_id = entry.entry_id
        self._backup_path: str = entry.data[CONF_BACKUP_PATH]
        self._cache: dict[str, AgentBackup] | None = None
        self._cache_expiration: float = 0.0

    def _remote(self, filename: str) -> str:
        return f"{self._backup_path}/{filename}"

    # ------------------------------------------------------------------
    # Upload -- der gehaertete Kern
    # ------------------------------------------------------------------

    @_handle_errors
    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: Callable[..., None] | None = None,
        **kwargs: Any,
    ) -> None:
        tar_name, meta_name = _suggested_filenames(backup)
        remote_tar = self._remote(tar_name)
        remote_meta = self._remote(meta_name)

        def _progress(n: int) -> None:
            if on_progress is None:
                return
            try:
                on_progress(bytes_uploaded=n)
            except TypeError:
                # Toleranz gegen abweichende Signaturen in anderen HA-Versionen.
                on_progress(n)

        attempts = max(1, self._client.attempts)
        for attempt in range(1, attempts + 1):
            stream = await open_stream()
            try:
                await self._client.upload(stream, remote_tar, backup.size, _progress)
                remote_size = await self._client.propfind_size(remote_tar)
                if remote_size != backup.size:
                    raise NcStalledError(
                        f"Groessen-Mismatch nach Upload: remote={remote_size} erwartet={backup.size}"
                    )
                _LOGGER.info(
                    "nc_backup: Upload von %s erfolgreich (%s Bytes, Versuch %s/%s)",
                    remote_tar, backup.size, attempt, attempts,
                )
                break
            except RETRYABLE_EXC as err:
                _LOGGER.warning(
                    "nc_backup: Upload-Versuch %s/%s fuer %s fehlgeschlagen (%s): %s",
                    attempt, attempts, backup.backup_id, type(err).__name__, err,
                )
                try:
                    await self._client.delete(remote_tar)
                except Exception:  # noqa: BLE001 -- Aufraeumen darf den Fehler nicht verdecken
                    pass
                if attempt == attempts:
                    raise BackupAgentError(
                        f"Upload nach {attempts} Versuchen fehlgeschlagen: {err}"
                    ) from err
                await asyncio.sleep(backoff_delay(attempt))
            finally:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:  # noqa: BLE001
                        pass

        # Metadaten ZULETZT: ein Tar ohne Metadatei ist fuer async_list_backups
        # unsichtbar und damit retention-immun (waechst still). Umgekehrt waere
        # schlimmer: ein sichtbares Backup, dessen Tar-Download 404 liefert.
        try:
            await self._client.put_bytes(
                remote_meta, json.dumps(backup.as_dict()).encode(), content_type="application/json"
            )
        except Exception as err:
            try:
                await self._client.delete(remote_tar)
            except Exception:  # noqa: BLE001
                pass
            raise BackupAgentError(f"Metadaten-Upload fehlgeschlagen: {err}") from err

        self._cache = None

    # ------------------------------------------------------------------
    # Liste / Lesen / Loeschen
    # ------------------------------------------------------------------

    async def _get_backups(self) -> dict[str, AgentBackup]:
        now = wall_time()
        if self._cache is not None and now < self._cache_expiration:
            return self._cache

        entries = await self._client.list_dir(self._backup_path)
        out: dict[str, AgentBackup] = {}
        for name, _size, is_collection in entries:
            if is_collection or not name.endswith(".metadata.json"):
                continue
            try:
                raw = await self._client.get_bytes(self._remote(name))
                backup = AgentBackup.from_dict(json.loads(raw))
            except Exception as err:  # noqa: BLE001 -- eine kaputte Datei darf die Liste nicht sprengen
                _LOGGER.warning("nc_backup: Metadaten %s unlesbar: %s", name, err)
                continue
            out[backup.backup_id] = backup

        self._cache = out
        self._cache_expiration = now + CACHE_TTL_S
        return out

    @_handle_errors
    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        return list((await self._get_backups()).values())

    @_handle_errors
    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        backups = await self._get_backups()
        if backup_id not in backups:
            raise BackupNotFound(f"Backup {backup_id} nicht gefunden")
        return backups[backup_id]

    @_handle_errors
    async def async_download_backup(self, backup_id: str, **kwargs: Any) -> AsyncIterator[bytes]:
        backup = await self.async_get_backup(backup_id)
        tar_name, _meta_name = _suggested_filenames(backup)
        return await self._client.download_stream(self._remote(tar_name))

    @_handle_errors
    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        backup = await self.async_get_backup(backup_id)
        tar_name, meta_name = _suggested_filenames(backup)
        await self._client.delete(self._remote(tar_name))
        await self._client.delete(self._remote(meta_name))
        if self._cache is not None:
            self._cache.pop(backup_id, None)
