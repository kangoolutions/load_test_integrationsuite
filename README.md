# CPI Load Tester

Dockerbasiertes Lasttest-Tool für SAP Cloud Integration: Ordner wählen, Endpunkt setzen, Takt vorgeben. Jede Datei geht **genau einmal** per HTTPS an den iFlow – mit Statuscode, Latenz und `SAP_MessageProcessingLogID` pro Nachricht. Ausgelegt auf sechsstellige Dateimengen pro Ordner (z. B. ~280.000 Dateien).

## Schnellstart

```bash
cp .env.example .env            # HOST_DATA_DIR auf den Ordner mit den Testdateien setzen
docker compose up -d --build
open http://localhost:8090
```

`HOST_DATA_DIR` wird **read-only** nach `/data` gemountet und ist die Wurzel des Ordner-Browsers. Ergebnisse liegen im Volume `cpiload_state` (SQLite) und überleben Neustarts.

## Ablauf in der Oberfläche

1. **Verbindung** – *Service Key einfügen* (JSON der Process Integration Runtime, Plan `integration-flow`) setzt Client-ID, Secret und Token-URL; die Endpoint-URL wird mit `<url>/http/` vorbelegt, den iFlow-Pfad ergänzen. *Token testen* prüft OAuth2 Client Credentials.
2. **Dateien** – Ordner unter `/data` wählen, Filter setzen (`*.xml, *.edi`, case-insensitive), optional Unterordner. Die Trefferzahl wird live gezählt. Sortierung ist natürlich (`msg_2` vor `msg_10`) – ein Smoke-Test mit N Dateien trifft immer dieselben ersten N.
3. **Last** – Parallelität, Rate-Limit (msg/s, gleichmäßiger Takt ohne Bursts), *Max. Dateien* für Smoke-Tests (Chips 1/5/10/100), Timeout, Methode, Content-Type (`auto` = nach Dateiendung), Auto-Pause nach N Fehlern in Folge, Platzhalter, Custom Header.
4. **Lauf starten** – Live-Dashboard mit Fortschritt, Durchsatz, p50/p95/p99, Restzeit, Statuscodes und den letzten 50 Fehlern (MPL-ID per Klick kopieren). Parallelität und Rate-Limit lassen sich **während des Laufs** nachregeln.
5. **Historie** – CSV-Export (alle bzw. nur Fehler; Semikolon, Excel-tauglich), *Fortsetzen* gestoppter/unterbrochener Läufe (nur offene Dateien), *Fehler senden* (neuer Lauf nur mit den fehlgeschlagenen Dateien).

## Platzhalter

| Platzhalter | Wert |
|---|---|
| `{{uuid}}` | UUID v4 pro Request |
| `{{seq}}` | laufende Nummer der Datei im Lauf |
| `{{filename}}` | Dateiname ohne Pfad |
| `{{timestamp}}` | ISO-8601 UTC, z. B. `2026-09-28T12:00:00.123Z` |
| `{{epoch_ms}}` | Unix-Zeit in ms |

Im Payload nur bei aktivierter Option und für UTF-8-Inhalte (Binärdateien bleiben unverändert), in Header-Werten immer. Body und Header eines Requests teilen sich dieselben Werte – z. B. `SAP_ApplicationID: {{uuid}}` findet sich identisch im Payload wieder.

## Hinweise für Lasttests gegen CPI

- **Smoke-Test zuerst** (5–10 Dateien), MPL-IDs im Monitor gegenprüfen, dann Rate schrittweise erhöhen.
- **Log-Level** des iFlows für den Lauf auf *Info* bzw. *None* – 280k MPLs mit *Debug/Trace* belasten den Tenant und die MPL-Retention.
- **Tenant-Limits** im Blick behalten: JMS-Queue-Kapazität bei asynchroner Entkopplung, maximale Message-Größe, Worker-Auslastung. Ein 429/503-Anstieg im Dashboard ist das Signal zum Nachregeln.
- **Duplikatprüfung/Idempotenz** im iFlow (z. B. Idempotent Process Call auf eine ID im Payload) greift bei Wiederholungen mit denselben Dateien – dafür `{{uuid}}` im Payload verwenden.
- **Auto-Pause** schützt vor 280k Fehlern in Sekunden (abgelaufene Credentials, iFlow undeployed). Nach der Ursachenbehebung einfach *Fortsetzen*.
- Bei **Stop/Pause** werden laufende Requests abgewartet, *Fortsetzen* sendet nichts doppelt. Nur ein harter Container-Abbruch kann Requests, die gerade unterwegs waren, beim Fortsetzen erneut senden (max. = Parallelität).

## Große Ordner (280k Dateien) unter Docker Desktop

Gemessen mit 280.000 Dateien in einem Ordner auf macOS:

| Schritt | Docker-Desktop-Bind-Mount | nativ / Linux-Host |
|---|---|---|
| Ordner einlesen | ~40 s (einmalig, danach Cache) | < 1 s |
| Lauf anlegen (280k Items in SQLite) | ~2 s | ~2 s |
| Ergebnisse schreiben / CSV-Export | ~4 s / ~2 s | – |

Das Einlesen bremst das File-Sharing von Docker Desktop, nicht das Tool. Deshalb wird jeder Ordner **einmal** gelesen und gecacht, bis sich sein Inhalt ändert (mtime). Browser, Vorschau und Start nutzen dasselbe Listing, *Neu einlesen* verwirft den Cache. Wer es schneller braucht:

- Daten einmalig in ein Docker-Volume kopieren und dieses statt des Bind-Mounts nach `/data` hängen, oder
- das Tool auf einem Linux-Host bzw. ohne Docker betreiben (siehe unten).

Testdaten **nicht in OneDrive/SharePoint-synchronisierten Ordnern** ablegen: 280k Dateien belasten den Sync-Client, und „Nur online verfügbare“ Dateien werden beim Lesen erst heruntergeladen.

## Lokale Entwicklung

```bash
uv sync
uv run pytest
uv run python scripts/gen_testdata.py --count 20000 --out testdata/sample
uv run uvicorn cpiload.main:app --reload --port 8080
```

Ohne Docker gelten `./testdata` als Datenordner und `./state/cpiload.db` als Datenbank (überschreibbar über `CPILOAD_DATA_DIR`, `CPILOAD_DB_PATH`).

## Aufbau

```
src/cpiload/
  main.py        FastAPI: REST-API, SSE-Livestream (/api/events), statische UI
  runner.py      Lauf-Engine: Dispatcher, Gate (Parallelität), Pacer (msg/s), Pause/Stop/Resume
  auth.py        OAuth2 Client Credentials: Token-Cache, Refresh vor Ablauf, Retry bei 401
  files.py       Ordner-Browser und Scan (os.scandir, Filter, natürliche Sortierung, Limit)
  templating.py  Platzhalter
  stats.py       Live-Kennzahlen: Log-Histogramm für Perzentile, Durchsatzfenster, Zeitreihe
  store.py       SQLite: runs + run_items, gebündelte Schreibzugriffe, CSV-Streaming
static/          Oberfläche (Vanilla JS) im Kangoolutions-Design, Assets unter static/ds/
```

## Fehlerbilder

| Code im Dashboard | Bedeutung |
|---|---|
| `401` | Token abgelehnt – wird einmal automatisch erneuert; bleibt es, fehlt die Rolle `ESBMessaging.send` am Service Key |
| `403` | CSRF-Schutz im HTTPS-Sender aktiv – für Lasttests deaktivieren |
| `404` | iFlow nicht deployed oder Pfad falsch |
| `413` | Payload über dem Limit des HTTPS-Senders |
| `429`/`503` | Tenant drosselt – Rate reduzieren |
| `TIMEOUT` / `CONN` | Keine Antwort in der Timeout-Zeit bzw. Verbindungsfehler |
| `AUTH` | Token-Endpunkt nicht erreichbar oder Credentials falsch. Bei `401 Bad credentials` mit Werten aus der `.env`: Secret in **einfache** Anführungszeichen setzen – ein `$` im Secret wird sonst von Compose als Variable aufgelöst |
| `FILE` | Datei nicht lesbar (Rechte im Mount prüfen) |
