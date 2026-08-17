"""Gehaerteter WebDAV-Client fuer den nc_backup-Agenten.

Bewusst kein aiowebdav2 (das nutzt die Core-`webdav`-Integration) --
wir brauchen direkten Zugriff auf den Request-Body-Iterator fuer den
Stall-Watchdog, den eine fertige WebDAV-Bibliothek nicht durchreicht.
Alles auf aiohttp aufgebaut (in HA-Core enthalten, keine zusaetzliche
Dependency in manifest.json noetig).

Kernidee Stall-Watchdog: aiohttp konsumiert einen AsyncIterator-Body
nur so schnell, wie der Socket geleert wird (Backpressure). "Seit N
Sekunden kein Chunk abgeholt" ist damit ein exakter Detektor fuer
einen haengenden Upload -- das kann ein ClientTimeout(total=...)
nicht leisten (der schlaegt erst nach dem vollen Limit zu, selbst
wenn der Upload nach Sekunde 1 komplett steht).
"""
from __future__ import annotations

import asyncio
import logging
import random
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from time import time as wall_time
from urllib.parse import quote
from uuid import uuid4

import aiohttp
from aiohttp import ClientTimeout

from .const import (
    BACKOFF_STEPS_S,
    DEFAULT_CHUNK_TOTAL_S,
    DEFAULT_CONNECT_S,
    DEFAULT_CTRL_TOTAL_S,
    DEFAULT_MOVE_TOTAL_S,
    FATAL_STATUS_CODES,
    NC_MAX_CHUNKS,
    NC_MIN_CHUNK_BYTES,
)

_LOGGER = logging.getLogger(__name__)

_PROPFIND_SIZE_BODY = (
    b'<?xml version="1.0"?>\n'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength/>'
    b"</d:prop></d:propfind>"
)
_PROPFIND_LIST_BODY = (
    b'<?xml version="1.0"?>\n'
    b'<d:propfind xmlns:d="DAV:">'
    b"<d:prop><d:getcontentlength/><d:resourcetype/></d:prop>"
    b"</d:propfind>"
)
_DAV_NS = "{DAV:}"


class NcBackupClientError(Exception):
    """Basisklasse aller Client-Fehler."""


class NcAuthError(NcBackupClientError):
    """401/403 -- Zugangsdaten falsch. Nie retrybar."""


class NcNotFoundError(NcBackupClientError):
    """404 -- Pfad/Ressource existiert nicht."""


class NcStalledError(NcBackupClientError):
    """Upload machte laenger als stall_timeout keinen Fortschritt,
    oder ein sonstiger transienter Fehler (Timeout/Connection/5xx)."""


def is_retryable_status(status: int) -> bool:
    """True, wenn ein erneuter Versuch bei diesem HTTP-Status sinnvoll ist."""
    if status in FATAL_STATUS_CODES:
        return False
    return status >= 400


def classify_status(status: int) -> None:
    """Wirft die passende Exception fuer einen fehlerhaften Status, oder
    tut nichts bei Erfolg (< 300)."""
    if status < 300:
        return
    if status in (401, 403):
        raise NcAuthError(f"HTTP {status}: Zugangsdaten abgelehnt")
    if status == 404:
        raise NcNotFoundError(f"HTTP {status}: nicht gefunden")
    if not is_retryable_status(status):
        raise NcBackupClientError(f"HTTP {status}: nicht behebbarer Fehler")
    raise NcStalledError(f"HTTP {status}")


# Von einem Versuch zum naechsten retrybare Ausnahmen (Netz-/Transient-Fehler).
RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    NcStalledError,
    TimeoutError,
    asyncio.TimeoutError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,
    aiohttp.ClientPayloadError,
)


def backoff_delay(attempt: int) -> float:
    """Backoff-Sekunden vor Versuch `attempt+1` (attempt ist 1-basiert),
    mit +/-20% Jitter gegen synchronisierte Retries."""
    idx = min(attempt - 1, len(BACKOFF_STEPS_S) - 1)
    base = BACKOFF_STEPS_S[idx]
    return base * (1 + random.uniform(-0.2, 0.2))


def rechunk(chunk_bytes: int) -> Callable[[AsyncIterator[bytes]], AsyncIterator[bytes]]:
    """Baut einen Re-Chunker: liest einen Byte-Strom und gibt Bloecke fester
    Groesse zurueck (letzter Block darf kleiner sein). Puffert nur EINEN
    Chunk im RAM -- die Gesamtdatei (~5GB) wird nie komplett gehalten."""

    async def _rechunk(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        buf = bytearray()
        async for piece in source:
            buf += piece
            while len(buf) >= chunk_bytes:
                yield bytes(buf[:chunk_bytes])
                del buf[:chunk_bytes]
        if buf:
            yield bytes(buf)

    return _rechunk


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


@dataclass
class NcWebDavClient:
    """Ein Client-Objekt pro Config Entry. Haelt keine eigene Session
    (die kommt vom Aufrufer, damit HA sie beim Entry-Unload sauber
    schliessen kann)."""

    session: aiohttp.ClientSession
    base_url: str  # z.B. https://nc.fhcld.at/remote.php/dav/files/Fabian
    username: str
    password: str
    backup_path: str
    verify_ssl: bool = True
    use_chunked: bool = True
    chunk_mb: int = 64
    stall_timeout_s: float = 300
    attempts: int = 4
    request_total_s: float = 7200

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        # Defensive Coercion: `range(1, self.attempts + 1)` verlangt int.
        # Live gefunden: HAs NumberSelector liefert im Config-Flow einen float
        # (4.0) zurueck -> TypeError. config_flow.py coerct das inzwischen
        # selbst, dieser zweite Schutzwall greift auch bei jedem anderen
        # Aufrufer (z.B. direkte Tests/zukuenftige Integrationen).
        self.attempts = int(self.attempts)
        self._auth = aiohttp.BasicAuth(self.username, self.password)
        self._ssl = None if self.verify_ssl else False
        # Nextclouds Chunked-Upload-v2-Sitzungen (`/uploads/<user>/<tid>/`)
        # haengen an der DAV-WURZEL, nicht unter `files/<user>/...` -- sie sind
        # ein GESCHWISTER von `files/`, kein Unterordner davon. `base_url`
        # zeigt aber auf .../dav/files/<user>. Live gefunden: mit `_url()`
        # (also base_url + "/uploads/...") baute das faelschlich einen Pfad
        # UNTER files/<user>/uploads/... -- Nextcloud quittierte das mit
        # HTTP 409 (Conflict), nicht mit dem erwarteten 201.
        if "/files/" in self.base_url:
            self._dav_root = self.base_url.rsplit("/files/", 1)[0]
        else:  # pragma: no cover -- Fallback fuer untypische URL-Formen
            self._dav_root = self.base_url

    # ------------------------------------------------------------------
    # Low-Level-Helfer
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def _dav_url(self, path: str) -> str:
        """Wie `_url()`, aber relativ zur DAV-Wurzel statt zu `files/<user>`
        -- fuer die Chunked-Upload-Sitzung, siehe Kommentar in __post_init__."""
        return f"{self._dav_root}{path if path.startswith('/') else '/' + path}"

    def _final_destination(self, remote_path: str) -> str:
        """Absolute Ziel-URL fuer den `Destination`-Header beim Chunked-Upload
        (MKCOL/MOVE). `base_url` zeigt bereits auf .../dav/files/<user> -- der
        Destination-Header braucht denselben `/dav/files/<user>`-Pfad, nur mit
        `remote_path` statt dem Upload-Session-Pfad angehaengt."""
        return self._url(remote_path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        total_s: float = DEFAULT_CTRL_TOTAL_S,
    ) -> aiohttp.ClientResponse:
        """Request relativ zu `base_url` (.../dav/files/<user>) -- fuer alles,
        was innerhalb des Backup-Zielordners liegt."""
        return await self._request_abs(method, self._url(path), headers=headers, data=data, total_s=total_s)

    async def _request_abs(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        total_s: float = DEFAULT_CTRL_TOTAL_S,
    ) -> aiohttp.ClientResponse:
        """Request gegen eine bereits vollstaendig aufgeloeste URL -- fuer die
        Chunked-Upload-Sitzung, die ausserhalb von `base_url` liegt."""
        timeout = ClientTimeout(connect=DEFAULT_CONNECT_S, total=total_s)
        resp = await self.session.request(
            method,
            url,
            headers=headers,
            data=data,
            timeout=timeout,
            auth=self._auth,
            ssl=self._ssl,
        )
        return resp

    async def _put_with_watchdog(
        self,
        *,
        url: str,
        body: AsyncIterator[bytes],
        length: int,
        headers: dict[str, str],
        total_s: float,
        on_bytes: Callable[[int], None],
    ) -> int:
        """Streaming-PUT mit Fortschritts-Watchdog. Bricht selbst ab
        (statt auf den vollen `total_s`-Timeout zu warten), sobald
        `stall_timeout_s` lang kein Byte mehr geflossen ist. Gibt den
        HTTP-Status zurueck."""
        loop = asyncio.get_running_loop()
        state = {"last": loop.time(), "sent": 0}

        async def _tracked() -> AsyncIterator[bytes]:
            async for chunk in body:
                state["last"] = loop.time()
                state["sent"] += len(chunk)
                on_bytes(state["sent"])
                yield chunk
            state["last"] = loop.time()

        timeout = ClientTimeout(connect=DEFAULT_CONNECT_S, total=total_s)
        req_task = asyncio.ensure_future(
            self.session.put(
                url,
                data=_tracked(),
                headers={**headers, "Content-Length": str(length)},
                timeout=timeout,
                auth=self._auth,
                ssl=self._ssl,
            )
        )

        async def _watchdog() -> None:
            interval = min(10.0, max(1.0, self.stall_timeout_s / 5))
            while True:
                await asyncio.sleep(interval)
                if loop.time() - state["last"] > self.stall_timeout_s:
                    req_task.cancel()
                    return

        wd_task = asyncio.ensure_future(_watchdog())
        try:
            resp = await req_task
        except asyncio.CancelledError:
            if wd_task.done():
                raise NcStalledError(
                    f"kein Fortschritt seit {self.stall_timeout_s}s "
                    f"bei {state['sent']}/{length} Bytes"
                ) from None
            raise
        finally:
            wd_task.cancel()
            try:
                await wd_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        async with resp:
            await resp.read()
            return resp.status

    # ------------------------------------------------------------------
    # Verzeichnis-Verwaltung
    # ------------------------------------------------------------------

    async def ensure_path(self, path: str) -> None:
        """Legt `path` (und alle Elternordner) per MKCOL an, falls noetig.
        405 (Method Not Allowed) heisst "existiert bereits" -- kein Fehler."""
        parts = [p for p in path.strip("/").split("/") if p]
        cur = ""
        for part in parts:
            cur = f"{cur}/{part}"
            resp = await self._request("MKCOL", cur, total_s=DEFAULT_CTRL_TOTAL_S)
            async with resp:
                if resp.status in (401, 403):
                    raise NcAuthError(f"MKCOL {cur}: HTTP {resp.status}")
                # 405 ist der RFC-4918-konforme Code fuer "Collection existiert
                # bereits"; manche Nextcloud-Versionen/Reverse-Proxies liefern
                # dafuer stattdessen 409. Beides hier tolerieren -- die
                # anschliessende PROPFIND in check_path() ist die eigentliche
                # Bestaetigung, ob der Pfad nutzbar ist.
                if resp.status not in (201, 405, 409):
                    raise NcBackupClientError(f"MKCOL {cur}: HTTP {resp.status}")

    async def check_path(self, path: str) -> bool:
        """Preflight fuer den Config Flow: Pfad existiert (oder wird
        angelegt) und ist per PROPFIND erreichbar."""
        await self.ensure_path(path)
        resp = await self._request(
            "PROPFIND",
            path,
            headers={"Depth": "0", "Content-Type": "application/xml"},
            data=_PROPFIND_SIZE_BODY,
            total_s=DEFAULT_CTRL_TOTAL_S,
        )
        async with resp:
            classify_status(resp.status)
            return resp.status < 300

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    async def upload(
        self,
        stream: AsyncIterator[bytes],
        remote_path: str,
        size: int,
        progress: Callable[[int], None],
    ) -> None:
        """Ein einzelner Upload-Versuch (kein Retry ueber den gesamten
        Stream -- das macht der Aufrufer per erneutem `open_stream()`,
        siehe backup.py). Innerhalb eines Chunked-Uploads wird aber jeder
        einzelne Chunk retryed, ohne den Quell-Stream neu zu lesen."""
        if self.use_chunked and size > NC_MIN_CHUNK_BYTES:
            await self._upload_chunked(stream, remote_path, size, progress)
        else:
            await self._upload_single(stream, remote_path, size, progress)

    async def _upload_single(
        self,
        stream: AsyncIterator[bytes],
        remote_path: str,
        size: int,
        progress: Callable[[int], None],
    ) -> None:
        status = await self._put_with_watchdog(
            url=self._url(remote_path),
            body=stream,
            length=size,
            headers={"Content-Type": "application/octet-stream"},
            total_s=self.request_total_s,
            on_bytes=progress,
        )
        classify_status(status)

    async def _upload_chunked(
        self,
        stream: AsyncIterator[bytes],
        remote_path: str,
        size: int,
        progress: Callable[[int], None],
    ) -> None:
        chunk_bytes = max(NC_MIN_CHUNK_BYTES, self.chunk_mb * 1024 * 1024)
        est_chunks = (size // chunk_bytes) + 1
        if est_chunks > NC_MAX_CHUNKS:
            # Chunkgroesse automatisch vergroessern statt hart zu scheitern.
            chunk_bytes = (size // NC_MAX_CHUNKS) + 1

        tid = f"ha-nc-backup-{uuid4()}"
        # WICHTIG: die Upload-Session liegt an der DAV-WURZEL (Geschwister von
        # files/<user>/), nicht unter base_url -- siehe Kommentar in
        # __post_init__. _dav_url() statt _url()/_request() fuer alles, was
        # sich auf diese Session bezieht (MKCOL/PUT-Chunks/MOVE/DELETE).
        upload_base = f"/uploads/{quote(self.username)}/{tid}"
        headers = {"Destination": self._final_destination(remote_path), "OC-Total-Length": str(size)}

        resp = await self._request_abs("MKCOL", self._dav_url(upload_base), headers=headers, total_s=DEFAULT_CTRL_TOTAL_S)
        async with resp:
            if resp.status not in (201, 405):
                classify_status(resp.status)

        try:
            idx = 1
            sent = 0
            splitter = rechunk(chunk_bytes)
            async for block in splitter(stream):
                if idx > NC_MAX_CHUNKS:
                    raise NcBackupClientError(
                        f"Mehr als {NC_MAX_CHUNKS} Chunks noetig -- chunk_mb erhoehen"
                    )
                block_start = sent
                for att in range(1, self.attempts + 1):
                    try:
                        status = await self._put_with_watchdog(
                            url=self._dav_url(f"{upload_base}/{idx:05d}"),
                            body=_once(block),
                            length=len(block),
                            headers=headers,
                            total_s=DEFAULT_CHUNK_TOTAL_S,
                            on_bytes=lambda n, base=block_start: progress(base + n),
                        )
                        classify_status(status)
                        break
                    except RETRYABLE_EXC as err:
                        if att == self.attempts:
                            raise
                        _LOGGER.debug(
                            "Chunk %s Versuch %s/%s fehlgeschlagen: %s",
                            idx, att, self.attempts, err,
                        )
                        await asyncio.sleep(backoff_delay(att))
                sent += len(block)
                progress(sent)
                idx += 1

            move_headers = {**headers, "X-OC-Mtime": str(int(wall_time()))}
            resp = await self._request_abs(
                "MOVE", self._dav_url(f"{upload_base}/.file"),
                headers=move_headers, total_s=DEFAULT_MOVE_TOTAL_S,
            )
            async with resp:
                classify_status(resp.status)
        except BaseException:
            try:
                resp = await self._request_abs("DELETE", self._dav_url(upload_base), total_s=DEFAULT_CTRL_TOTAL_S)
                async with resp:
                    pass
            except Exception:  # noqa: BLE001
                pass
            raise

    # ------------------------------------------------------------------
    # Verifikation / Verwaltung
    # ------------------------------------------------------------------

    async def propfind_size(self, remote_path: str) -> int | None:
        """Groesse der Datei am Ziel, oder None wenn sie nicht existiert."""
        resp = await self._request(
            "PROPFIND",
            remote_path,
            headers={"Depth": "0", "Content-Type": "application/xml"},
            data=_PROPFIND_SIZE_BODY,
            total_s=DEFAULT_CTRL_TOTAL_S,
        )
        async with resp:
            if resp.status == 404:
                return None
            classify_status(resp.status)
            body = await resp.text()
        return _parse_content_length(body)

    async def list_dir(self, path: str) -> list[tuple[str, int | None, bool]]:
        """Liste (name, size, is_collection) der direkten Kinder von `path`."""
        resp = await self._request(
            "PROPFIND",
            path,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            data=_PROPFIND_LIST_BODY,
            total_s=DEFAULT_CTRL_TOTAL_S,
        )
        async with resp:
            if resp.status == 404:
                return []
            classify_status(resp.status)
            body = await resp.text()
        return _parse_dir_listing(body, path)

    async def get_bytes(self, remote_path: str) -> bytes:
        resp = await self._request("GET", remote_path, total_s=DEFAULT_CTRL_TOTAL_S)
        async with resp:
            classify_status(resp.status)
            return await resp.read()

    async def download_stream(self, remote_path: str) -> AsyncIterator[bytes]:
        resp = await self._request("GET", remote_path, total_s=self.request_total_s)
        classify_status(resp.status)

        async def _iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.content.iter_chunked(65536):
                    yield chunk
            finally:
                resp.close()

        return _iter()

    async def put_bytes(
        self, remote_path: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        """Kleiner Ein-Schuss-Upload (Metadaten-JSON o.ae.) mit eigenem Retry."""
        last_err: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                status = await self._put_with_watchdog(
                    url=self._url(remote_path),
                    body=_once(data),
                    length=len(data),
                    headers={"Content-Type": content_type},
                    total_s=DEFAULT_CTRL_TOTAL_S,
                    on_bytes=lambda _n: None,
                )
                classify_status(status)
                return
            except RETRYABLE_EXC as err:
                last_err = err
                if attempt == self.attempts:
                    raise
                _LOGGER.debug("put_bytes %s Versuch %s fehlgeschlagen: %s", remote_path, attempt, err)
                await asyncio.sleep(backoff_delay(attempt))
        if last_err:  # pragma: no cover -- durch die raise oben eigentlich unerreichbar
            raise last_err

    async def delete(self, remote_path: str) -> None:
        resp = await self._request("DELETE", remote_path, total_s=DEFAULT_CTRL_TOTAL_S)
        async with resp:
            if resp.status == 404:
                return
            classify_status(resp.status)


def _parse_content_length(xml_body: str) -> int | None:
    root = ET.fromstring(xml_body)  # noqa: S314 -- eigener, TLS-gesicherter, authentifizierter Server
    el = root.find(f".//{_DAV_NS}getcontentlength")
    if el is not None and el.text:
        return int(el.text)
    return None


def _parse_dir_listing(xml_body: str, base_path: str) -> list[tuple[str, int | None, bool]]:
    root = ET.fromstring(xml_body)  # noqa: S314
    base_norm = base_path.rstrip("/")
    out: list[tuple[str, int | None, bool]] = []
    for resp_el in root.findall(f"{_DAV_NS}response"):
        href_el = resp_el.find(f"{_DAV_NS}href")
        if href_el is None or not href_el.text:
            continue
        href = href_el.text.rstrip("/")
        if href.rstrip("/").endswith(base_norm.rstrip("/")):
            continue  # der Ordner selbst, nicht sein Inhalt
        name = href.rsplit("/", 1)[-1]
        from urllib.parse import unquote
        name = unquote(name)
        size_el = resp_el.find(f".//{_DAV_NS}getcontentlength")
        size = int(size_el.text) if size_el is not None and size_el.text else None
        is_collection = resp_el.find(f".//{_DAV_NS}resourcetype/{_DAV_NS}collection") is not None
        out.append((name, size, is_collection))
    return out
