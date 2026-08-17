"""Reine Logik-Tests fuer client.py -- kein laufendes Home Assistant noetig
(siehe conftest.py). Deckt genau die in Plan-Abschnitt 9d geforderten
Faelle: rechunk()-Grenzen, Stall-Watchdog, Retry-Klassifikation,
PROPFIND-Parser."""
from __future__ import annotations

import asyncio

import pytest

from nc_backup.client import (
    NcAuthError,
    NcBackupClientError,
    NcNotFoundError,
    NcStalledError,
    NcWebDavClient,
    _parse_content_length,
    _parse_dir_listing,
    backoff_delay,
    classify_status,
    is_retryable_status,
    rechunk,
)
from nc_backup.const import BACKOFF_STEPS_S


# ---------------------------------------------------------------------
# rechunk()
# ---------------------------------------------------------------------


async def _source(data: bytes, piece_size: int):
    for i in range(0, len(data), piece_size):
        yield data[i : i + piece_size]


async def test_rechunk_preserves_all_bytes_exact_multiple():
    data = bytes(range(256)) * 40  # 10240 Bytes, exaktes Vielfaches von 1024
    splitter = rechunk(1024)
    out = bytearray()
    sizes = []
    async for block in splitter(_source(data, 37)):  # krumme Quell-Stueckelung
        sizes.append(len(block))
        out += block
    assert bytes(out) == data
    assert sizes[:-1] == [1024] * (len(sizes) - 1)
    assert sizes[-1] == 1024  # exaktes Vielfaches -> letzter Block auch voll


async def test_rechunk_last_block_smaller():
    data = b"x" * 2500
    splitter = rechunk(1024)
    sizes = []
    async for block in splitter(_source(data, 300)):
        sizes.append(len(block))
    assert sizes == [1024, 1024, 452]
    assert sum(sizes) == len(data)


async def test_rechunk_smaller_than_one_chunk():
    data = b"abc"
    splitter = rechunk(1024)
    out = [block async for block in splitter(_source(data, 1))]
    assert out == [b"abc"]


# ---------------------------------------------------------------------
# Status-Klassifikation
# ---------------------------------------------------------------------


def test_classify_status_success_is_noop():
    classify_status(200)
    classify_status(201)
    classify_status(204)


def test_classify_status_auth_errors():
    with pytest.raises(NcAuthError):
        classify_status(401)
    with pytest.raises(NcAuthError):
        classify_status(403)


def test_classify_status_not_found():
    with pytest.raises(NcNotFoundError):
        classify_status(404)


def test_classify_status_fatal_quota_not_retryable():
    with pytest.raises(NcBackupClientError) as exc_info:
        classify_status(507)
    assert not isinstance(exc_info.value, NcStalledError)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_classify_status_transient_is_retryable(status):
    with pytest.raises(NcStalledError):
        classify_status(status)


@pytest.mark.parametrize("status", [401, 403, 404, 405, 409, 507])
def test_is_retryable_status_false_for_fatal(status):
    assert is_retryable_status(status) is False


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_is_retryable_status_true_for_transient(status):
    assert is_retryable_status(status) is True


# ---------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------


@pytest.mark.parametrize("attempt", [1, 2, 3, 4, 10])
def test_backoff_delay_within_jitter_bounds(attempt):
    idx = min(attempt - 1, len(BACKOFF_STEPS_S) - 1)
    base = BACKOFF_STEPS_S[idx]
    delay = backoff_delay(attempt)
    assert base * 0.8 <= delay <= base * 1.2


# ---------------------------------------------------------------------
# DAV-Wurzel vs. files/<user>-Pfad -- Live-Fund: Chunked-Upload-Sitzungen
# (`/uploads/<user>/...`) haengen an der DAV-Wurzel, nicht unter
# `files/<user>/...`. Erste Version baute sie faelschlich unter base_url
# (= .../dav/files/<user>) -- Nextcloud quittierte das live mit HTTP 409.
# ---------------------------------------------------------------------


def test_attempts_coerced_to_int_even_if_float_passed_in():
    """Live-Fund: HAs NumberSelector lieferte im Config-Flow einen float
    (4.0) fuer 'attempts' -- `range(1, 4.0 + 1)` wirft TypeError. Der
    Client muss unabhaengig vom Aufrufer robust bleiben."""
    client = NcWebDavClient(
        session=object(),
        base_url="https://example.invalid/dav/files/u",
        username="u",
        password="p",
        backup_path="/x",
        attempts=4.0,
    )
    assert client.attempts == 4
    assert isinstance(client.attempts, int)
    range(1, client.attempts + 1)  # darf nicht mehr TypeError werfen


def test_dav_root_strips_files_user_suffix():
    client = NcWebDavClient(
        session=object(),  # in diesem Test unbenutzt
        base_url="https://nc.fhcld.at/remote.php/dav/files/Fabian/",
        username="Fabian",
        password="p",
        backup_path="/Backups/HomeAssistant",
    )
    assert client._dav_root == "https://nc.fhcld.at/remote.php/dav"
    assert (
        client._dav_url("/uploads/Fabian/xyz")
        == "https://nc.fhcld.at/remote.php/dav/uploads/Fabian/xyz"
    )
    assert (
        client._url("/Backups/HomeAssistant/foo.tar")
        == "https://nc.fhcld.at/remote.php/dav/files/Fabian/Backups/HomeAssistant/foo.tar"
    )


# ---------------------------------------------------------------------
# Stall-Watchdog -- der eigentliche Fix aus dem Plan
# ---------------------------------------------------------------------


class _HangingSession:
    """Simuliert einen PUT, der nach dem Verbindungsaufbau nie mehr
    antwortet (Body wird nie konsumiert) -- genau das Live-Symptom
    ('Backup operation timed out' nach 12h)."""

    async def put(self, *args, **kwargs):
        await asyncio.sleep(1e9)


async def test_put_with_watchdog_detects_stall_fast():
    client = NcWebDavClient(
        session=_HangingSession(),
        base_url="https://example.invalid/dav",
        username="u",
        password="p",
        backup_path="/x",
        stall_timeout_s=0.05,
        attempts=1,
    )

    async def _never_progresses():
        if False:  # nie ausgefuehrt -- die Session konsumiert den Body ohnehin nicht
            yield b""

    with pytest.raises(NcStalledError):
        await asyncio.wait_for(
            client._put_with_watchdog(
                url="https://example.invalid/dav/foo.tar",
                body=_never_progresses(),
                length=10,
                headers={},
                total_s=999,
                on_bytes=lambda n: None,
            ),
            timeout=5,
        )


# ---------------------------------------------------------------------
# PROPFIND-Parser
# ---------------------------------------------------------------------

_PROPFIND_SIZE_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/remote.php/dav/files/Fabian/Backups/HomeAssistant/foo.tar</d:href>
    <d:propstat>
      <d:prop><d:getcontentlength>12345</d:getcontentlength></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""

_PROPFIND_LIST_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/remote.php/dav/files/Fabian/Backups/HomeAssistant/</d:href>
    <d:propstat>
      <d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/Fabian/Backups/HomeAssistant/backup1.tar</d:href>
    <d:propstat>
      <d:prop><d:getcontentlength>500</d:getcontentlength><d:resourcetype/></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/Fabian/Backups/HomeAssistant/backup1.metadata.json</d:href>
    <d:propstat>
      <d:prop><d:getcontentlength>42</d:getcontentlength><d:resourcetype/></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""


def test_parse_content_length():
    assert _parse_content_length(_PROPFIND_SIZE_XML) == 12345


def test_parse_dir_listing_excludes_base_and_reads_children():
    entries = _parse_dir_listing(_PROPFIND_LIST_XML, "/Backups/HomeAssistant")
    names = {name for name, _size, _is_dir in entries}
    assert names == {"backup1.tar", "backup1.metadata.json"}

    by_name = {name: (size, is_dir) for name, size, is_dir in entries}
    assert by_name["backup1.tar"] == (500, False)
    assert by_name["backup1.metadata.json"] == (42, False)
