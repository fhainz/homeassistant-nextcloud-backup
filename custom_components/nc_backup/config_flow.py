"""Config- und Options-Flow fuer nc_backup.

Uebernimmt Zugangsdaten wahlweise 1:1 aus einem bestehenden `webdav`-
Config-Entry (einmalige Kopie, keine Laufzeit-Kopplung -- verschwindet der
webdav-Entry spaeter, bleibt dieser Agent unbeeintraechtigt) oder per
manueller Eingabe. Ein echter Preflight (Pfad anlegen/pruefen + 8MB
Testupload ueber denselben Chunked-Pfad wie im Ernstfall) deckt kaputte
Proxy-Konfigurationen VOR dem ersten 5GB-Nachtlauf auf.
"""
from __future__ import annotations

import logging
import random
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

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
    DEFAULT_BACKUP_PATH,
    DEFAULT_CHUNK_MB,
    DEFAULT_REQUEST_TOTAL_S,
    DEFAULT_STALL_TIMEOUT_S,
    DEFAULT_USE_CHUNKED,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_PROBE_SUFFIX = "/.nc_backup_probe"
_PROBE_SIZE = 8 * 1024 * 1024  # 8 MB -- gross genug, um Chunking zu testen


async def _bytes_once(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _host(url: str) -> str:
    return urlparse(url).netloc or url


def _int_selector(**config: Any) -> vol.All:
    """NumberSelector liefert im Config-Flow-Schema einen float (z.B. 4.0)
    zurueck, auch bei step=1 -- Live-Fund: `range(1, self.attempts + 1)`
    mit self.attempts=4.0 wirft `TypeError: 'float' object cannot be
    interpreted as an integer`. vol.Coerce(int) danach erzwingt echten int."""
    return vol.All(NumberSelector(NumberSelectorConfig(**config)), vol.Coerce(int))


def _tuning_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_USE_CHUNKED, default=defaults.get(CONF_USE_CHUNKED, DEFAULT_USE_CHUNKED)
            ): BooleanSelector(),
            vol.Optional(
                CONF_CHUNK_MB, default=defaults.get(CONF_CHUNK_MB, DEFAULT_CHUNK_MB)
            ): _int_selector(min=5, max=1024, step=1, mode=NumberSelectorMode.BOX),
            vol.Optional(
                CONF_STALL_TIMEOUT,
                default=defaults.get(CONF_STALL_TIMEOUT, DEFAULT_STALL_TIMEOUT_S),
            ): _int_selector(min=60, max=3600, step=10, mode=NumberSelectorMode.BOX),
            vol.Optional(
                CONF_ATTEMPTS, default=defaults.get(CONF_ATTEMPTS, DEFAULT_ATTEMPTS)
            ): _int_selector(min=1, max=10, step=1, mode=NumberSelectorMode.BOX),
            vol.Optional(
                CONF_REQUEST_TOTAL,
                default=defaults.get(CONF_REQUEST_TOTAL, DEFAULT_REQUEST_TOTAL_S),
            ): _int_selector(min=600, max=43200, step=100, mode=NumberSelectorMode.BOX),
        }
    )


class NcBackupConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Setup-Assistent: Quelle waehlen -> ggf. manuell eingeben -> Tuning -> Preflight."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._tuning: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        webdav_entries = self.hass.config_entries.async_entries("webdav")

        if user_input is not None:
            source = user_input["source"]
            if source == "manual":
                return await self.async_step_credentials()
            entry = next((e for e in webdav_entries if e.entry_id == source), None)
            if entry is None:
                errors["base"] = "source_not_found"
            else:
                self._data = {
                    CONF_URL: entry.data[CONF_URL],
                    CONF_USERNAME: entry.data[CONF_USERNAME],
                    CONF_PASSWORD: entry.data[CONF_PASSWORD],
                    CONF_BACKUP_PATH: entry.data.get(CONF_BACKUP_PATH, DEFAULT_BACKUP_PATH),
                    CONF_VERIFY_SSL: entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                }
                return await self.async_step_tuning()

        options = [
            {"value": e.entry_id, "label": f"{e.title} ({e.data.get(CONF_URL, '?')})"}
            for e in webdav_entries
        ]
        options.append({"value": "manual", "label": "Zugangsdaten manuell eingeben"})
        schema = vol.Schema(
            {
                vol.Required("source", default=options[0]["value"]): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_credentials(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = {
                CONF_URL: user_input[CONF_URL].rstrip("/") + "/",
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_BACKUP_PATH: user_input.get(CONF_BACKUP_PATH, DEFAULT_BACKUP_PATH),
                CONF_VERIFY_SSL: user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            }
            return await self.async_step_tuning()

        schema = vol.Schema(
            {
                vol.Required(CONF_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                vol.Required(CONF_USERNAME): TextSelector(),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_BACKUP_PATH, default=DEFAULT_BACKUP_PATH): TextSelector(),
                vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="credentials", data_schema=schema, errors=errors)

    async def async_step_tuning(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._tuning = user_input
            return await self.async_step_validate()
        return self.async_show_form(step_id="tuning", data_schema=_tuning_schema({}))

    async def async_step_validate(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        verify_ssl = self._data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
        session = async_get_clientsession(self.hass, verify_ssl=verify_ssl)
        client = NcWebDavClient(
            session=session,
            base_url=self._data[CONF_URL],
            username=self._data[CONF_USERNAME],
            password=self._data[CONF_PASSWORD],
            backup_path=self._data[CONF_BACKUP_PATH],
            verify_ssl=verify_ssl,
            use_chunked=self._tuning.get(CONF_USE_CHUNKED, DEFAULT_USE_CHUNKED),
            chunk_mb=self._tuning.get(CONF_CHUNK_MB, DEFAULT_CHUNK_MB),
            stall_timeout_s=self._tuning.get(CONF_STALL_TIMEOUT, DEFAULT_STALL_TIMEOUT_S),
            attempts=self._tuning.get(CONF_ATTEMPTS, DEFAULT_ATTEMPTS),
            request_total_s=self._tuning.get(CONF_REQUEST_TOTAL, DEFAULT_REQUEST_TOTAL_S),
        )
        probe_path = f"{self._data[CONF_BACKUP_PATH]}{_PROBE_SUFFIX}"
        try:
            await client.check_path(self._data[CONF_BACKUP_PATH])
            probe_data = random.randbytes(_PROBE_SIZE)
            await client.upload(_bytes_once(probe_data), probe_path, len(probe_data), lambda _n: None)
            size = await client.propfind_size(probe_path)
            if size != len(probe_data):
                errors["base"] = "probe_mismatch"
        except NcAuthError:
            errors["base"] = "invalid_auth"
        except (TimeoutError, NcBackupClientError, OSError) as err:
            _LOGGER.warning("nc_backup: Preflight fehlgeschlagen: %s", err)
            errors["base"] = "cannot_connect"
        finally:
            try:
                await client.delete(probe_path)
            except Exception:  # noqa: BLE001 -- Aufraeumen darf den eigentlichen Fehler nicht verdecken
                pass

        if errors:
            return self.async_show_form(step_id="validate", data_schema=vol.Schema({}), errors=errors)

        await self.async_set_unique_id(f"{self._data[CONF_USERNAME]}@{_host(self._data[CONF_URL])}")
        self._abort_if_unique_id_configured()

        title = f"{self._data[CONF_USERNAME]}@{_host(self._data[CONF_URL])}"
        return self.async_create_entry(title=title, data=self._data, options=self._tuning)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> NcBackupOptionsFlow:
        return NcBackupOptionsFlow(config_entry)


class NcBackupOptionsFlow(config_entries.OptionsFlow):
    """Timeouts/Chunkgroesse/Attempts nachtraeglich aenderbar -- genau der
    Mangel, der beim Core-`webdav`-Agenten eine komplette Neuanlage
    (inkl. App-Passwort) erzwang."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(step_id="init", data_schema=_tuning_schema(self._entry.options))
