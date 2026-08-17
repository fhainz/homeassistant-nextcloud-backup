<p align="center">
  <img src="icon.png" width="96" height="96" alt="Nextcloud Backup Icon">
</p>

<h1 align="center">Nextcloud Backup</h1>

<p align="center">
  Hardened WebDAV backup agent for Home Assistant — a drop-in replacement
  for the built-in <code>webdav</code> backup integration for large
  backups to slow/unstable Nextcloud servers.
</p>

<p align="center">
  <a href="https://github.com/hacs/integration"><img alt="HACS Custom" src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg"></a>
  <img alt="License" src="https://img.shields.io/badge/license-MIT-blue.svg">
  <img alt="Home Assistant" src="https://img.shields.io/badge/Home%20Assistant-2024.12%2B-41BDF5.svg">
</p>

## Why

Home Assistant's built-in `webdav` backup integration uploads backups
with a single, unchunked HTTP PUT and waits up to 12 hours
(`BACKUP_TIMEOUT = ClientTimeout(total=43200)`) before giving up. With
large backups (several GB, including the database) to a slow or
unstable server — e.g. reseller hosting — that PUT can **stall with zero
bytes still moving**, and only dies once the full 12-hour limit is hit.
A bigger timeout doesn't help (it's already big enough) — what's needed
is a progress watchdog.

This custom component replaces `webdav` as the backup target, using the
same credentials but hardened upload behavior:

- **Stall watchdog**: aborts the upload itself once a configurable
  amount of time has passed with **no measured progress** (default 5
  minutes) — instead of waiting hours on a fixed timeout.
- **Chunked upload** (Nextcloud Chunked Upload v2): large backups are
  uploaded in configurable blocks, each retried/backed-off individually
  — a single failed block is retried, not the whole upload. Memory usage
  stays capped at one block size instead of the full backup size.
- **PROPFIND size verification** after upload instead of blindly trusting
  "done".
- **Compatible with existing `webdav` backups**: identical filename
  convention — the new agent immediately sees backups already uploaded
  by the built-in `webdav` agent. Rolling back to `webdav` is always a
  pure configuration change, no data migration needed.

## Installation

### Via HACS (recommended)

1. HACS → Integrations → menu (⋮) → *Custom repositories*.
2. Add this project's repository URL, category *Integration*.
3. Install "Nextcloud Backup", restart Home Assistant.

### Manual

1. Copy this folder (`custom_components/nc_backup/`) to
   `<config>/custom_components/nc_backup/`.
2. **Restart** Home Assistant (custom components are only detected at
   core startup, a plain entry reload is not enough).

## Setup

Settings → Devices & Services → Add Integration → *Nextcloud Backup*.

- **Credentials**: either take them over from an existing `webdav`
  integration (one-time copy, no runtime coupling) or enter them
  manually (WebDAV URL, username, app password recommended).
- A real **preflight test** (create/verify target folder + 8 MB test
  upload over the same chunked path used in production) runs before
  the setup finishes — catches broken proxy configurations before the
  first real nightly run fails.

Afterwards, select the new agent under **Settings → System → Backups →
Automatic backups → Locations** as a target (and deselect the old
`webdav` entry there, if any).

### Options (changeable later)

| Field | Default | Meaning |
|---|---|---|
| Use chunked upload | on | Nextcloud Chunked Upload v2 instead of a single PUT |
| Chunk size | 64 MB | Size per upload block |
| Stall timeout | 300 s | No progress for this long → abort + retry |
| Attempts | 4 | Retries on transient errors |
| Total timeout (single upload) | 7200 s | Only relevant when chunking is disabled |

## Limitations

- Tested against Nextcloud (WebDAV + Chunked Upload v2). Other WebDAV
  servers without chunked upload support only work with chunking
  disabled (single PUT + stall watchdog).
- No automatic migration of old backups between agents is needed (see
  above), but there's also no automatic migration of configuration —
  selecting the target in HA's backup settings is a manual step.

## Development

```bash
python3 -m pytest tests/ -q
```

Pure logic tests (`client.py` deliberately has no `homeassistant`
dependency, runs without Home Assistant installed).

## License

[MIT](LICENSE)
