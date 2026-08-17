<p align="center">
  <img src="icon.png" width="96" height="96" alt="Nextcloud Backup Icon">
</p>

<h1 align="center">Nextcloud Backup</h1>

<p align="center">
  Gehärteter WebDAV-Backup-Agent für Home Assistant — als Ersatz für die
  eingebaute <code>webdav</code>-Backup-Integration bei großen Backups zu
  langsamen/instabilen Nextcloud-Servern.
</p>

<p align="center">
  <a href="https://github.com/hacs/integration"><img alt="HACS Custom" src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg"></a>
  <img alt="License" src="https://img.shields.io/badge/license-MIT-blue.svg">
  <img alt="Home Assistant" src="https://img.shields.io/badge/Home%20Assistant-2024.12%2B-41BDF5.svg">
</p>

## Warum

Home Assistants eingebaute `webdav`-Backup-Integration lädt Backups mit
einem einzigen, ungestückelten HTTP-PUT hoch und wartet dabei bis zu
12 Stunden (`BACKUP_TIMEOUT = ClientTimeout(total=43200)`), bevor sie
aufgibt. Bei großen Backups (mehrere GB, inkl. Datenbank) zu einem
langsamen oder instabilen Server — z. B. Reseller-Hosting — kann dieser
PUT **stecken bleiben, ohne dass ein Byte mehr fließt**, und stirbt dann
erst am vollen 12-Stunden-Limit. Ein größerer Timeout hilft nicht (der ist
schon groß genug) — gebraucht wird ein Fortschritts-Watchdog.

Dieses Custom Component ersetzt `webdav` als Backup-Ziel mit denselben
Zugangsdaten, aber gehärtetem Upload-Verhalten:

- **Stall-Watchdog**: bricht den Upload selbst ab, sobald eine
  konfigurierbare Zeit lang **kein Fortschritt** mehr gemessen wurde
  (Default 5 Minuten) — statt stundenlang auf ein festes Timeout zu warten.
- **Chunked-Upload** (Nextcloud Chunked Upload v2): große Backups werden in
  konfigurierbaren Blöcken hochgeladen, jeder Block einzeln mit
  Retry/Backoff — ein einzelner fehlgeschlagener Block wiederholt sich,
  nicht der gesamte Upload. RAM-Verbrauch bleibt auf eine Blockgröße
  begrenzt, nicht auf die volle Backup-Größe.
- **PROPFIND-Größenverifikation** nach dem Upload statt blindem "fertig".
- **Kompatibel mit vorhandenen `webdav`-Backups**: identische
  Dateinamenskonvention — der neue Agent sieht Backups, die der
  eingebaute `webdav`-Agent bereits hochgeladen hat, sofort mit. Ein
  Rollback zurück auf `webdav` ist jederzeit eine reine
  Konfigurationsänderung, kein Datenumzug nötig.

## Installation

### Über HACS (empfohlen)

1. HACS → Integrationen → Menü (⋮) → *Benutzerdefinierte Repositories*.
2. Repository-URL dieses Projekts eintragen, Kategorie *Integration*.
3. „Nextcloud Backup" installieren, Home Assistant neu starten.

### Manuell

1. Diesen Ordner (`custom_components/nc_backup/`) nach
   `<config>/custom_components/nc_backup/` kopieren.
2. Home Assistant **neu starten** (Custom Components werden nur beim
   Core-Start erkannt, ein reines Entry-Reload reicht nicht).

## Einrichtung

Einstellungen → Geräte & Dienste → Integration hinzufügen → *Nextcloud
Backup*.

- **Zugangsdaten**: entweder von einer bestehenden `webdav`-Integration
  übernehmen (einmalige Kopie, keine Laufzeit-Kopplung) oder manuell
  eingeben (WebDAV-URL, Benutzername, App-Passwort empfohlen).
- Ein echter **Preflight-Test** (Zielordner anlegen/prüfen + 8-MB-
  Testupload über denselben Chunked-Pfad wie im Ernstfall) läuft vor dem
  Abschluss — deckt kaputte Proxy-Konfigurationen auf, bevor der erste
  echte Nachtlauf scheitert.

Danach den neuen Agenten in **Einstellungen → System → Backups →
automatische Backups → Speicherorte** als Ziel auswählen (bzw. den alten
`webdav`-Eintrag dort abwählen).

### Optionen (nachträglich änderbar)

| Feld | Standard | Bedeutung |
|---|---|---|
| Chunked-Upload verwenden | an | Nextcloud Chunked Upload v2 statt Einzel-PUT |
| Chunk-Größe | 64 MB | Größe je Upload-Block |
| Stillstand-Timeout | 300 s | Kein Fortschritt für diese Zeit → Abbruch + Retry |
| Versuche | 4 | Wiederholungen bei transienten Fehlern |
| Gesamt-Timeout Einzel-Upload | 7200 s | Nur relevant bei deaktiviertem Chunking |

## Grenzen

- Getestet gegen Nextcloud (WebDAV + Chunked Upload v2). Andere
  WebDAV-Server ohne Chunked-Upload-Unterstützung funktionieren nur mit
  deaktiviertem Chunking (Einzel-PUT + Stall-Watchdog).
- Kein automatischer Umzug alter Backups zwischen Agenten nötig (siehe
  oben), aber auch keine automatische Migration der Konfiguration —
  Zielauswahl in den HA-Backup-Einstellungen ist ein manueller Schritt.

## Entwicklung

```bash
python3 -m pytest tests/ -q
```

Reine Logik-Tests (`client.py` hat bewusst keine `homeassistant`-Abhängigkeit,
läuft ohne installiertes Home Assistant).

## Lizenz

[MIT](LICENSE)
