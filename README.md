<p align="right"><img src="app/static/img/buschehome-logo-128.webp" width="96" height="96" alt="BuscheHome-Logo" /></p>

# Docker Backup Manager

> **Version 1.5.1** — webbasiertes Backup- und Restore-Tool für Docker-Container

Ein selbst gehostetes Web-Interface zum Sichern und Wiederherstellen von Docker-Containern — einzeln oder als komplette Gruppe (Landscape). Backups lassen sich lokal, auf SMB-Freigaben (NAS), S3-kompatiblen Diensten, per SFTP, WebDAV oder FTP, in Azure, Google Drive, OneDrive, Dropbox und vielen weiteren Cloud-Zielen speichern. Die gesamte Konfiguration erfolgt im Browser, kein Kommandozeilen-Wissen nötig.

Das Tool spricht ausschließlich mit der **Docker Engine API** (über den Docker-Socket), nie mit der `docker`-CLI. Es läuft dadurch identisch auf Docker Desktop, Synology Container Manager, QNAP Container Station, UGREEN Docker-App oder purem Docker Engine auf Linux.

---

## Inhaltsverzeichnis

- [Features](#features)
- [Architektur](#architektur)
- [Backup-Format](#backup-format)
- [Installation](#installation)
  - [Linux / Docker Compose](#1-linux--docker-compose)
  - [Synology NAS (DSM 7)](#2-synology-nas-dsm-7)
  - [QNAP NAS](#3-qnap-nas)
  - [UGREEN NAS](#4-ugreen-nas)
  - [Portainer (Stacks)](#5-portainer-stacks)
  - [Windows / Docker Desktop](#6-windows--docker-desktop)
  - [Ohne Docker (Entwicklung)](#7-ohne-docker-entwicklung)
- [Erste Schritte](#erste-schritte)
- [Backups erstellen](#backups-erstellen)
  - [Einzelnen Container sichern](#einzelnen-container-sichern)
  - [Ganze Landscape sichern](#ganze-landscape-sichern)
  - [Container stoppen (anwendungskonsistent)](#container-stoppen-anwendungskonsistent)
  - [Volumes direkt streamen](#volumes-direkt-streamen)
- [Zeitpläne & Aufbewahrung](#zeitpläne--aufbewahrung)
- [Wiederherstellen](#wiederherstellen)
  - [Einzelnen Container wiederherstellen](#einzelnen-container-wiederherstellen)
  - [Gruppe wiederherstellen (Landscape)](#gruppe-wiederherstellen-landscape)
  - [Auf einem anderen Host wiederherstellen](#auf-einem-anderen-host-wiederherstellen)
- [Speicherziele (Offsite-Kopien)](#speicherziele-offsite-kopien)
  - [SMB / CIFS (Windows-Freigabe / NAS)](#smb--cifs-windows-freigabe--nas)
  - [Lokaler / gemounteter Pfad](#lokaler--gemounteter-pfad)
  - [S3-kompatibel](#s3-kompatibel)
  - [SFTP / SSH](#sftp--ssh)
  - [WebDAV](#webdav)
  - [FTP / FTPS](#ftp--ftps)
  - [Azure Blob Storage](#azure-blob-storage)
  - [Dropbox](#dropbox)
  - [Weitere Cloud-Dienste](#weitere-cloud-dienste)
  - [Google Drive](#google-drive)
  - [OneDrive](#onedrive)
  - [rclone (Dropbox, SFTP, WebDAV, …)](#rclone-dropbox-sftp-webdav-)
  - [Katalog importieren](#katalog-importieren)
- [Verschlüsselung](#verschlüsselung)
- [Sicherheit & Benutzerverwaltung](#sicherheit--benutzerverwaltung)
  - [Login & Brute-Force-Schutz](#login--brute-force-schutz)
  - [Session-Timeout](#session-timeout)
  - [Mehrere Benutzer anlegen (Benutzerverwaltung)](#mehrere-benutzer-anlegen-benutzerverwaltung)
  - [Passwort zurücksetzen](#passwort-zurücksetzen)
  - [Betrieb hinter einem Reverse-Proxy](#betrieb-hinter-einem-reverse-proxy)
- [Kopplung mit NetPulse (Monitoring)](#kopplung-mit-netpulse-monitoring)
- [Logs](#logs)
- [Umgebungsvariablen – Referenz](#umgebungsvariablen--referenz)
- [Entwicklung & Tests](#entwicklung--tests)
- [Autor, Marke & Lizenzhinweis](#autor-marke--lizenzhinweis)

---

## Features

| Bereich | Funktion |
|---|---|
| **Backup** | Einzelne Container oder komplette Docker-Landscape (alle Container oder gefiltert nach Compose-Projekt / Namensbestandteil) |
| **Backup-Inhalt** | Docker-Image (`docker save`), alle benannten Volumes, Bind-Mounts (Host-Ordner, nicht nur der Verweis), Custom-Netzwerke, vollständige Container-Konfiguration |
| **Restore** | Auf demselben oder einem anderen Host/OS; Container-Name anpassbar; Landscape-Gruppe auf einmal oder nur einzelne Mitglieder |
| **Parallel-Restore** | Landscape auf einem Parallel-System wiederherstellen mit Name-Präfix (z. B. `staging_`) ohne Produktionssystem zu berühren |
| **Zeitpläne** | Stündlich / täglich / wöchentlich / monatlich + Uhrzeit, kein Cron-Wissen nötig; Aufbewahrungsrichtlinie (Anzahl Versionen + Alter in Tagen) |
| **Speicherziele** | SMB/CIFS, lokaler/gemounteter Pfad, S3-kompatibel (mit Vorlagen für Backblaze B2, Wasabi, Hetzner, Cloudflare R2, MinIO, …), SFTP, WebDAV, FTP/FTPS, Azure Blob, Google Drive, OneDrive, Dropbox, pCloud, Box, Mega, Koofr, Storj, rclone-Remote aus eigener rclone.conf |
| **Streaming** | Volume-Daten direkt ans Speicherziel, ohne lokalen Zwischenspeicher (für große Volumes wie Immich-Mediatheken) |
| **Verschlüsselung** | AES-256-CBC + HMAC-SHA256 at rest (Schlüssel nur per Umgebungsvariable) |
| **Benutzerverwaltung** | Mehrere Benutzer, Admin- und Nutzer-Rolle; Admin kann Benutzer anlegen, löschen und entsperren |
| **Sicherheit** | Bcrypt-Passwörter, Brute-Force-Sperre, konfigurierbarer Session-Timeout, optionales HTTPS-only-Cookie |
| **Logs** | Persistente Ereignishistorie aller Backup-, Restore- und Zeitplan-Läufe |
| **UI** | Modernes responsives Web-Interface (hell/dunkel), läuft ohne Build-Schritt, Fortschrittsanzeige für laufende Jobs |
| **Versionierung** | Jedes Backup ist eine eigene Zeitstempel-Version; automatisches Löschen der Offsite-Kopien beim Entfernen einer Version |

---

## Architektur

```
Browser  ──►  FastAPI (Python)  ──►  Docker Engine API (Socket)
                    │
                    ├──► SQLite (Metadaten, Zeitpläne, Benutzer)
                    ├──► APScheduler (Zeitplan-Ausführung)
                    ├──► restic (inkrementelle Deduplizierung, Volume-Streaming)
                    └──► rclone (SMB, S3, Google Drive, OneDrive, Dropbox, …)
```

- **Backend:** Python 3.12 / FastAPI, SQLite, APScheduler
- **Backup-Engine:** Alpine-Hilfscontainer für `tar`-Archivierung; `restic` für Deduplizierung und direktes Streaming; `rclone` für externe Ziele
- **Frontend:** reines HTML/CSS/JavaScript ohne Build-Schritt (kein Node, kein Webpack)
- **Deployment:** ein einzelner Docker-Container, Daten unter `/data`

---

## Backup-Format

Backups sind portabel und menschenlesbar — kein proprietäres Format:

```
/data/backups/
└── <container_name>/
    └── <YYYYMMDD_HHMMSS>/
        ├── meta.json           Metadaten (Format-Version, Backup-Typ, Docker-Version, …)
        ├── container.json      Vollständiges `docker inspect`
        ├── image.tar           `docker save` des Images
        ├── networks.json       Konfiguration angehängter Custom-Netzwerke
        ├── volumes/
        │   └── <name>.tar.gz   Inhalt jedes benannten Volumes
        └── binds/
            └── <pfad>.tar.gz   Inhalt jedes Bind-Mounts (Host-Ordner)
```

Ein **Landscape-Backup** ist eine Sammlung solcher Container-Backups plus einem Verzeichnis `_landscapes/<label>/<timestamp>/` mit Metadaten als Verknüpfung. Die Mitglieds-Container-Backups sind normale Container-Backups und können unabhängig wiederhergestellt werden.

Beim **direkten Volume-Streaming** (restic-Modus) landen die Volume-Daten nicht lokal, sondern in einem restic-Repository auf dem Speicherziel (`<ziel>/docker-backup/<container>/restic_repo`).

---

## Installation

### 1. Linux / Docker Compose

```bash
git clone https://github.com/sirbuschi2003/docker-backup-manager.git
cd docker-backup-manager
docker compose up -d --build
```

Danach: `http://<server-ip>:8420` im Browser öffnen und Admin-Konto anlegen.

Das Verzeichnis `./data` neben der `docker-compose.yml` enthält die Datenbank und alle Backups — regelmäßig sichern.

---

### 2. Synology NAS (DSM 7, Container Manager)

1. Über **Container Manager → Projekt → Erstellen** ein neues Projekt anlegen.
2. Pfad wählen (z. B. `/docker/docker-backup-manager`) und die Dateien per File Station hochladen: `docker-compose.yml`, `Dockerfile`, `requirements.txt` und den Ordner `app/`.
3. Als Quelle „Docker-Compose-YAML erstellen/importieren" wählen.
4. Projekt starten, danach `http://<nas-ip>:8420` aufrufen.

Der Volume-Pfad `./data` zeigt automatisch in den Synology-Projektordner (z. B. `/volume1/docker/docker-backup-manager/data`). Der Docker-Socket liegt bei Synology unter `/var/run/docker.sock` — der Compose-Mount ist bereits korrekt gesetzt.

---

### 3. QNAP NAS

1. **Container Station → Anwendungen erstellen → Docker-Compose YAML erstellen**.
2. `docker-compose.yml` einfügen; `./data` auf einen QNAP-Freigabeordner anpassen (z. B. `/share/Container/dbm/data`).
3. QNAP exponiert den Docker-Socket automatisch — der Standard-Mount funktioniert.
4. Erstellen & starten, danach `http://<nas-ip>:8420`.

---

### 4. UGREEN NAS

1. In der UGREEN Docker-App **Compose-Projekt anlegen**.
2. `docker-compose.yml` einfügen, `./data` auf einen Pfad im UGREEN-Datenpool anpassen.
3. Docker-Socket-Mount beibehalten.
4. Projekt starten, `http://<nas-ip>:8420` öffnen.

---

### 5. Portainer (Stacks)

**Option A — Git-Repository (empfohlen, ermöglicht „Pull & Redeploy"):**

1. **Stacks → Add stack**, Build method: **Repository**.
2. Repository-URL: `https://github.com/Sirbuschi2003/docker-backup-manager`
3. **Wichtig:** Repository reference auf `refs/heads/master` setzen (nicht `main`).
4. Unter **Environment variables** optional `DBM_TZ`, `DBM_SECRET_KEY` usw. eintragen.
5. **Deploy the stack** klicken.

**Option B — Fertig gebautes Image (kein lokaler Build nötig):**

Bei jedem Push auf `master` wird automatisch ein Image per GitHub Actions nach GHCR gebaut. Inhalt für den Web-Editor:

```yaml
services:
  docker-backup-manager:
    image: ghcr.io/sirbuschi2003/docker-backup-manager:latest
    container_name: docker-backup-manager
    restart: unless-stopped
    ports:
      - "8420:8420"
    environment:
      DBM_TZ: "${DBM_TZ:-UTC}"
      DBM_ENCRYPTION_KEY: "${DBM_ENCRYPTION_KEY:-}"
      DBM_SECRET_KEY: "${DBM_SECRET_KEY:-}"
      DBM_SESSION_HTTPS_ONLY: "${DBM_SESSION_HTTPS_ONLY:-false}"
      DBM_PUBLIC_URL: "${DBM_PUBLIC_URL:-}"
      DBM_GOOGLE_CLIENT_ID: "${DBM_GOOGLE_CLIENT_ID:-}"
      DBM_GOOGLE_CLIENT_SECRET: "${DBM_GOOGLE_CLIENT_SECRET:-}"
      DBM_MS_CLIENT_ID: "${DBM_MS_CLIENT_ID:-}"
      DBM_MS_CLIENT_SECRET: "${DBM_MS_CLIENT_SECRET:-}"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - dbm_data:/data
volumes:
  dbm_data:
```


---

### 6. Windows / Docker Desktop

```powershell
git clone https://github.com/sirbuschi2003/docker-backup-manager.git
cd docker-backup-manager
docker compose up -d --build
```

Docker Desktop muss laufen (WSL2-Backend empfohlen). Danach `http://localhost:8420` öffnen.

---

### 7. Ohne Docker (Entwicklung)

```bash
python -m venv .venv
# Linux/Mac:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt
DBM_BASE_DIR=./data uvicorn app.main:app --host 0.0.0.0 --port 8420
```

In diesem Modus muss der Rechner Zugriff auf einen laufenden Docker-Daemon haben (lokaler Docker Engine / Docker Desktop).

---

## Erste Schritte

1. **Admin-Konto anlegen:** Beim ersten Aufruf von `http://<host>:8420` erscheint ein Einrichtungsformular — Benutzernamen (min. 3 Zeichen) und Passwort (min. 8 Zeichen) eingeben. Dieser erste Benutzer erhält automatisch **Admin-Rechte**.

2. **Zeitzone einstellen:** Unter **Einstellungen** wird die aktuelle Serverzeit und Zeitzone angezeigt. Ist sie falsch (Standard: UTC), die Umgebungsvariable `DBM_TZ` auf die eigene IANA-Zeitzone setzen (z. B. `Europe/Berlin`) und den Container neu starten.

3. **Container-Übersicht:** Unter **Container** sind alle laufenden und gestoppten Container des Docker-Hosts sichtbar. Von dort kann sofort ein Backup gestartet werden.

4. **Speicherziele anlegen:** Für Offsite-Kopien unter **Einstellungen → Speicherziele** ein SMB-, S3- oder Cloud-Ziel einrichten (siehe [Speicherziele](#speicherziele-offsite-kopien)).

5. **Zeitpläne einrichten:** Unter **Zeitpläne** automatische Backups mit Aufbewahrungsrichtlinie und Speicherzielen konfigurieren.

---

## Backups erstellen

### Einzelnen Container sichern

1. **Container**-Seite öffnen.
2. Beim gewünschten Container auf **„Backup jetzt"** klicken.
3. Optional: Speicherziele auswählen (Checkboxen der konfigurierten Ziele) und/oder **„Volumes direkt streamen"** aktivieren.
4. **„Backup starten"** klicken.

Der Fortschritt erscheint als Ladebalken unten links in der App. Ein laufendes Backup kann dort jederzeit über **„Abbrechen"** gestoppt werden — der Job bricht am nächsten sicheren Checkpoint ab und räumt unvollständige Daten auf.

**Was wird gesichert:**
- Docker-Image (vollständig, portabel)
- Alle benannten Volumes (als `.tar.gz`)
- Alle Bind-Mounts / Host-Ordner (die tatsächlichen Dateien, nicht nur der Pfad-Verweis)
- Custom-Netzwerke und vollständige Container-Konfiguration

**Was wird nicht gesichert:**
- Docker-interne Bind-Mounts wie der Docker-Socket (`/var/run/docker.sock`) — werden automatisch ausgeschlossen
- Einzelne Dateien, die als Bind-Mount gemountet sind (z. B. `/etc/localtime`), werden mit einem Warnhinweis übersprungen — das Backup läuft trotzdem weiter

---

### Ganze Landscape sichern

Eine **Landscape** ist eine Gruppe zusammengehöriger Container — z. B. alle Container einer Nextcloud- oder Immich-Instanz.

1. Auf der **Container**-Seite: **„Gesamte Landschaft sichern"** klicken.
2. Optional nach **Compose-Projekt** filtern (z. B. `immich`), oder nach **Namensbestandteil** (z. B. `nextcloud-aio` für Anwendungen ohne Compose-Labels).
3. Speicherziele und Streaming-Option auswählen.
4. **„Backup starten"** klicken.

Landscape-Backups erscheinen unter **Backups** mit einem eigenen Eintrag. Über den Button **„Mitglieder"** sieht man alle enthaltenen Container mit Backup-Status und kann diese einzeln oder als Gruppe wiederherstellen.

---

### Container stoppen (anwendungskonsistent)

Standardmäßig läuft das Backup bei laufendem Container (*crash-konsistent* — für die meisten Anwendungen völlig ausreichend). Für Datenbanken oder kritische Dienste empfiehlt sich ein **anwendungskonsistentes Backup**:

- Im Backup-Dialog den Schalter **„Container vor dem Backup stoppen, danach wieder starten"** aktivieren.
- Der Container (bei Landscapes: jeder betroffene Container einzeln) wird dann kurz gestoppt, gesichert und direkt danach wieder gestartet.
- Bereits gestoppte Container werden nicht angetastet — kein ungewolltes Starten.
- Der Schalter ist auch in Zeitplänen konfigurierbar.

---

### Volumes direkt streamen

Standardmäßig wird ein Backup **zuerst lokal** unter `/data/backups` geschrieben, dann hochgeladen. Bei großen Volumes (z. B. eine Immich-Mediathek mit Hunderten Gigabyte) kann der lokale Platzbedarf ein Problem sein.

Mit **„Volumes direkt streamen, ohne lokal zu speichern"** gehen die Volume-Daten direkt aus dem Sicherungs-Container an das Speicherziel — ohne je lokal zu landen. Image und Metadaten bleiben klein und werden weiterhin lokal gespeichert.

**Unterstützte Ziele für Streaming:** Lokaler/gemounteter Pfad, SMB, S3, rclone  
**Nicht unterstützt:** Google Drive / OneDrive (deren API benötigt eine bekannte Dateigröße vorab)

> **Wichtig:** Beim direkten Streaming wird die AES-256-Verschlüsselung dieser App für die Volume-Daten **umgangen** (sie greift nur für lokal geschriebene Dateien). Nur nutzen, wenn dem Zielsystem selbst vertraut wird (z. B. eigenes NAS im LAN mit SMB3-Verschlüsselung).

Wiederherstellen und Löschen funktionieren transparent: die App lädt Volume-Daten bei Bedarf vom Speicherziel herunter bzw. löscht sie dort. Werden alle Backup-Versionen eines Containers auf einem Streaming-Ziel gelöscht, wird auch das restic-Repository auf dem Ziel automatisch entfernt.

---

## Zeitpläne & Aufbewahrung

Unter **Zeitpläne** lassen sich automatische Backups einrichten:

| Feld | Beschreibung |
|---|---|
| **Häufigkeit** | Alle X Stunden, täglich, wöchentlich oder monatlich + Uhrzeit |
| **Ziel** | Einzelner Container oder ganze Landscape (optional nach Projekt/Name filtern) |
| **Speicherziele** | Checkboxen: welche konfigurierten Ziele soll dieser Zeitplan verwenden? (Leer = nur lokal) |
| **Streaming** | Volume-Streaming für diesen Zeitplan aktivieren |
| **Container stoppen** | Anwendungskonsistentes Backup durch kurzes Stoppen |
| **Versionen behalten** | Anzahl neueste Versionen, die behalten werden (0 = unbegrenzt) |
| **Alter (Tage)** | Versionen älter als X Tage werden gelöscht (0 = deaktiviert) |

**Zeitzone:** Zeitpläne laufen standardmäßig in UTC. Ohne `DBM_TZ` läuft ein für 03:00 Uhr geplantes Backup tatsächlich um 03:00 UTC (in Deutschland 1–2 Stunden später). `DBM_TZ=Europe/Berlin` in der `docker-compose.yml` setzen und Container neu starten.

**Aufbewahrung:** Werden beim Prüfen der Aufbewahrungsregel Versionen gelöscht, werden automatisch auch die Offsite-Kopien auf allen Speicherzielen entfernt. Bei Landscape-Backups werden auch die Mitglieds-Container-Backups mit bereinigt.

**Log und Datenbankgröße:** Unter **Logs → Aufbewahrung** lässt sich einstellen, wie lange (Standard 90 Tage) und wie viele Einträge (Standard 50.000) das Log behält. Die Einstellung wird gespeichert und überlebt Neustarts. Jede Nacht um 03:30 werden ältere Einträge gelöscht und die Datenbankdatei verkleinert (sonntags vollständig per `VACUUM`). „Jetzt bereinigen“ macht beides sofort, die aktuelle Dateigröße steht oben auf der Logs-Seite. Startwerte ohne gespeicherte Einstellung: `DBM_LOG_RETENTION_DAYS`, `DBM_LOG_MAX_ENTRIES` (0 = unbegrenzt). Die Datenbank läuft im WAL-Modus. Liegt `/data` auf einem Netzlaufwerk (NFS/SMB), `DBM_DB_WAL=0` setzen.

---

## Wiederherstellen

### Einzelnen Container wiederherstellen

1. **Backups**-Seite öffnen, gewünschten Container und Version auswählen.
2. Auf **„Wiederherstellen"** klicken — das genaue Datum und die Uhrzeit des Backup-Stands werden vor der Bestätigung angezeigt.
3. Optional den Ziel-Containernamen anpassen (z. B. `mein_container_test` um Namenskonflikte zu vermeiden).
4. **„Wiederherstellen"** bestätigen.

Die Wiederherstellung deckt ab: Umgebungsvariablen, Ports, Volumes/Binds, Restart-Policy, Netzwerke, Capabilities, Privileged-Mode. Sehr exotische Host-Konfigurationen (komplexe Device-Mappings) müssen ggf. manuell nachjustiert werden.

---

### Gruppe wiederherstellen (Landscape)

Landscape-Backups ermöglichen zwei Modi:

**1. Ganzes Projekt auf einmal wiederherstellen (Überschreiben-Modus):**
- Unter **Backups** den Landscape-Eintrag wählen → **„Mitglieder"** klicken.
- **„Ganzes Projekt wiederherstellen"** wählen (Standard-Modus: Überschreiben).
- Bestätigen — alle enthaltenen Container werden der Reihe nach wiederhergestellt.

**2. Parallel-System / Staging-Restore:**
- Unter **Mitglieder** den Modus **„Parallel (Name-Präfix)"** wählen.
- Ein Präfix eingeben (z. B. `staging_` oder `test_`).
- **„Ganzes Projekt wiederherstellen"** klicken — alle Container erhalten den Präfix vor dem Namen (z. B. `staging_immich_server_01`). Das Produktivsystem bleibt unberührt.

**3. Einzelnen Mitglieds-Container wiederherstellen:**
- In der Mitglieder-Liste auf **„Einzeln"** klicken und wie gewohnt wiederherstellen.

---

### Auf einem anderen Host wiederherstellen

**Variante A — Katalog von Speicherziel importieren (empfohlen bei Totalverlust):**

1. Docker Backup Manager auf dem neuen Host installieren und starten.
2. Unter **Einstellungen → Speicherziele** dasselbe Speicherziel erneut anlegen (jeder Typ außer Google Drive und OneDrive), auf dem die alten Backups liegen.
3. Auf **„Katalog importieren"** klicken — die App durchsucht das Ziel nach vorhandenen Backup-Versionen und legt passende Einträge unter **Backups** an.
4. Aus der **Backups**-Liste die gewünschte Version wiederherstellen. Die Daten werden erst beim Klick auf „Wiederherstellen" automatisch heruntergeladen.

> Hinweis: Google Drive / OneDrive werden als Quelle für den Katalog-Import nicht unterstützt — dort Variante B nutzen.

**Variante B — Backup-Ordner manuell übertragen:**

1. Backup-Ordner (z. B. per `rclone copy`, SMB-Download oder S3-Sync) auf den Zielhost übertragen.
2. In die `/data/backups`-Verzeichnisstruktur des neuen Hosts einfügen.
3. In der UI unter **Backups** die Version auswählen und wiederherstellen.

---

## Speicherziele (Offsite-Kopien)

Unter **Einstellungen → Speicherziele** werden externe Backup-Ziele für Offsite-Kopien konfiguriert. Mehrere Ziele können gleichzeitig genutzt werden — pro Zeitplan und manuell wird gewählt, an welche Ziele hochgeladen werden soll.

### SMB / CIFS (Windows-Freigabe / NAS)

Das empfohlene Ziel für Heimnetzwerke und NAS-Systeme: Server-Adresse, Freigabename, Benutzername und Passwort direkt in der App eingeben — kein Host-Mount, kein privilegierter Container nötig.

- Auf **„Freigaben anzeigen"** klicken, um verfügbare Freigaben auf dem Server aufzulisten (statt den Namen zu erraten).
- Unterstützt SMB2/SMB3 mit Verschlüsselung.

### Lokaler / gemounteter Pfad

Für bereits auf Host-Ebene gemountete Freigaben (Synology/QNAP-Freigabenverwaltung, `/etc/fstab`): Pfad im Container eintragen, der per Volume-Mount eingereicht wird.

In der `docker-compose.yml` die auskommentierte Zeile aktivieren:

```yaml
volumes:
  - /mnt/nas-share:/mnt/remote-backup
```

Danach in den Einstellungen `/mnt/remote-backup` als lokalen Pfad eintragen.

### S3-kompatibel

Bucket, Endpoint (leer für AWS S3), Region, Access Key und Secret Key eintragen. Über **Anbieter-Vorlage** werden Endpoint und Region für Backblaze B2, Wasabi, Hetzner Object Storage, Cloudflare R2, MinIO, IDrive e2, Synology C2, Scaleway, OVHcloud und Google Cloud Storage (HMAC-Schlüssel) vorbelegt, mit einem Hinweis, wo die Zugangsdaten zu finden sind.

- **Adressierung:** „Pfad-Stil" für MinIO und andere Server, die per IP-Adresse erreicht werden.
- **Speicherklasse (nur AWS):** STANDARD, STANDARD_IA, ONEZONE_IA, INTELLIGENT_TIERING oder GLACIER_IR. Glacier Flexible Retrieval und Deep Archive werden bewusst nicht angeboten, weil eine Wiederherstellung erst nach Stunden möglich wäre.
- Die App schickt nur die Prüfsummen mit, die die S3-API verlangt. Neuere boto3-Versionen senden sonst standardmäßig zusätzliche CRC-Prüfsummen, die manche S3-kompatible Anbieter ablehnen.

### SFTP / SSH

Für Hetzner Storage Box (Port **23**), rsync.net, jeden Linux-Server oder ein zweites NAS. Host, Port, Benutzer und entweder Passwort oder privaten SSH-Schlüssel eintragen.

- **Host-Key (empfohlen):** Ausgabe von `ssh-keyscan -p PORT HOST` einfügen. Dann prüft die App, dass sie wirklich mit deinem Server spricht. Ohne Host-Key wird jeder Server-Schlüssel akzeptiert.
- Verschlüsselte Schlüssel kann rclone nur im alten PEM-Format entschlüsseln. Am einfachsten einen eigenen Schlüssel ohne Passphrase nur für Backups anlegen.
- Zielordner ohne führenden `/` ist relativ zum Home-Verzeichnis (bei der Storage Box nötig).

### WebDAV

Vorlagen für Nextcloud, ownCloud, MagentaCLOUD, IONOS/STRATO HiDrive, Koofr und SharePoint; „Anderer WebDAV-Server" für Synology, QNAP & Co. URL, Benutzer und (App-)Passwort eintragen. Bei Nextcloud/MagentaCLOUD ein App-Passwort statt des Login-Passworts verwenden.

### FTP / FTPS

Für die Fritz!Box (USB-Speicher als NAS) und ältere NAS-Geräte. Standard ist FTPS mit expliziter Verschlüsselung. „Zertifikat nicht prüfen" nur für selbst signierte Zertifikate im eigenen Netz aktivieren (z. B. Fritz!Box). Unverschlüsseltes FTP ist möglich, überträgt Passwort und Daten aber im Klartext.

### Azure Blob Storage

Speicherkonto + Zugriffsschlüssel oder alternativ eine SAS-URL, dazu Container und optional ein Präfix. Die Zugriffsebene (Hot/Cool/Cold) ist wählbar; „Archive" wird nicht angeboten, weil archivierte Daten vor einer Wiederherstellung erst stundenlang reaktiviert werden müssten.

### Dropbox

1. Unter [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps/create) eine App anlegen („Scoped access", „App folder" oder „Full Dropbox").
2. Unter *Permissions* `files.content.write`, `files.content.read` und `account_info.read` aktivieren.
3. App key und App secret im Dialog eintragen, **Bei Dropbox anmelden** klicken, Zugriff erlauben und den angezeigten Code einfügen, dann **Verbinden**.

Die Anmeldung braucht keine öffentliche Adresse und keine Redirect-URL, funktioniert also auch, wenn die App nur per `http://<LAN-IP>` erreichbar ist. Das Refresh-Token bleibt auf dem Server und wird nie an den Browser zurückgegeben.

### Weitere Cloud-Dienste

Typ **„Weitere Cloud"** bietet kleine Formulare für:

| Dienst | Was eintragen |
|---|---|
| pCloud | Region (EU/USA) und Token aus `rclone authorize "pcloud"` |
| Box | Token aus `rclone authorize "box"` |
| Mega | E-Mail und Passwort (Zwei-Faktor-Anmeldung muss aus sein) |
| Koofr | E-Mail und App-Passwort |
| Storj | Access Grant aus der Storj-Konsole |
| Anderes rclone-Backend | Backend-Name und Optionen (`schluessel = wert`), Werte für `pass`/`password` werden automatisch verschleiert |

Den Token für pCloud und Box erzeugt man einmalig auf einem beliebigen Rechner mit Browser: [rclone](https://rclone.org/downloads/) installieren, `rclone authorize "pcloud"` ausführen und die ausgegebene JSON-Zeile komplett einfügen.

**Technik:** SFTP, WebDAV, FTP, Azure, Dropbox und die weiteren Clouds laufen über das mitgelieferte rclone. Für jeden Vorgang erzeugt die App eine temporäre rclone.conf (Rechte 0600) aus den gespeicherten Einstellungen und löscht sie danach wieder. Alle diese Ziele unterstützen Volume-Streaming mit restic, Wiederherstellen, Löschen und den Katalog-Import.

### Google Drive

Ermöglicht Backup direkt in Google Drive ohne `rclone config` oder Konfigurationsdatei.

> ⚠️ **Google erlaubt keine privaten IP-Adressen** (10.x, 192.168.x, 172.16.x) als Redirect-URI. Als `DBM_PUBLIC_URL` muss daher eine der folgenden Optionen verwendet werden:
> - `http://localhost:8420` — nur wenn Browser und DBM auf **demselben** Rechner laufen
> - Ein lokaler Hostname, z.B. `http://dbm.home:8420` — eingetragen in lokaler DNS oder `/etc/hosts`
> - Ein echter Domain-Name mit Reverse Proxy
>
> **Einfachste Alternative:** Google Drive über den Speicherziel-Typ **rclone** einbinden — kein eigenes OAuth nötig, kein IP-Problem (siehe [rclone-Abschnitt](#rclone-dropbox-box-pcloud-mega-sftp-webdav-)).

**Einmaliges Setup (pro Installation, nicht pro Benutzer):**

1. [Google Cloud Console](https://console.cloud.google.com/) → Neues Projekt anlegen → **APIs & Services → Library → Google Drive API** aktivieren.
2. **APIs & Services → OAuth consent screen**: Typ „External", App-Namen vergeben. Im Testing-Status den eigenen Google-Account unter „Test users" eintragen.
3. **Credentials → Create Credentials → OAuth client ID** → Typ „Web application".
4. In den DBM-Einstellungen ein neues Speicherziel „Google Drive" anlegen — der **Einrichtungs-Assistent** zeigt die genaue Redirect-URI an und führt durch alle Schritte. Client-ID, Client-Secret und die DBM-Adresse lassen sich direkt in der Oberfläche eintragen — kein Neustart nötig.

> **Alternativ** als Umgebungsvariablen (haben Vorrang vor den UI-Einstellungen):
> ```
> DBM_GOOGLE_CLIENT_ID=<client-id>
> DBM_GOOGLE_CLIENT_SECRET=<client-secret>
> DBM_PUBLIC_URL=http://dbm.home:8420
> ```

### OneDrive

Funktioniert mit privaten Microsoft-/Outlook-Konten und Firmenkonten.

> **Fehlermeldung „außerhalb eines Verzeichnisses"?** Microsoft erfordert für App-Registrierungen ein Azure-Verzeichnis. Einmalig [kostenlos bei Azure registrieren](https://azure.microsoft.com/free/) (nur Identitätsprüfung, keine Kosten). Danach läuft die App-Registrierung normal.  
> **Alternative:** OneDrive über den Speicherziel-Typ **rclone** einbinden – kein Azure-Konto nötig, aber einmalige Einrichtung per `rclone config` erforderlich (siehe [rclone-Abschnitt](#rclone-dropbox-box-pcloud-mega-sftp-webdav-)).

**Einmaliges Setup:**

1. [Azure Portal](https://portal.azure.com/) → **App registrations → New registration**.  
   Bei „Supported account types": **Konten in einem beliebigen Organisationsverzeichnis und persönliche Microsoft-Konten** wählen.
2. **Certificates & secrets → New client secret** erzeugen.
3. **API permissions → Microsoft Graph → Delegated permissions**: `Files.ReadWrite` und `offline_access` hinzufügen.
4. In den DBM-Einstellungen ein neues Speicherziel „OneDrive" anlegen — der **Einrichtungs-Assistent** zeigt die genaue Redirect-URI an, die in Azure unter „Authentifizierung" einzutragen ist, und führt durch alle Schritte. Client-ID, Client-Secret und die DBM-Adresse lassen sich direkt in der Oberfläche eintragen — kein Neustart nötig.

> **Alternativ** als Umgebungsvariablen (haben Vorrang vor den UI-Einstellungen):
> ```
> DBM_MS_CLIENT_ID=<application-id>
> DBM_MS_CLIENT_SECRET=<client-secret>
> DBM_PUBLIC_URL=http://192.168.1.10:8420
> ```
> Für private Microsoft-Konten: `DBM_MS_TENANT=common` lassen (Standard).

### rclone (Dropbox, Box, pCloud, Mega, SFTP, WebDAV, …)

Für Experten, die eine vorhandene rclone.conf weiterverwenden möchten:

1. Lokal `rclone config` ausführen und einen Remote konfigurieren.
2. Die erzeugte `rclone.conf` in den Container einbinden:
   ```yaml
   volumes:
     - ./rclone.conf:/data/rclone.conf:ro
   ```
3. In den Einstellungen: Remote-Name (z. B. `meincloud`) und Zielpfad eintragen.

### Katalog importieren

Nach einem Totalverlust des Hosts genügt es, dasselbe Speicherziel erneut einzurichten und auf **„Katalog importieren"** zu klicken. Die App durchsucht das Ziel nach vorhandenen Backup-Versionen (erkennbar an `meta.json`) und legt passende Einträge unter **Backups** an — die eigentlichen Daten werden erst beim Klick auf „Wiederherstellen" heruntergeladen.

---

## Verschlüsselung

Backups können optional mit **AES-256-CBC + HMAC-SHA256** verschlüsselt auf der Platte abgelegt werden (encrypt-then-MAC, gestreamt in Blöcken — auch Gigabyte-große Archive brauchen keinen vollständigen RAM-Puffer).

**Einrichten:**

1. Schlüssel erzeugen:
   ```bash
   openssl rand -base64 32
   ```
2. Als Umgebungsvariable setzen:
   ```
   DBM_ENCRYPTION_KEY=<erzeugter-schlüssel>
   ```
3. Container neu starten. Ab dann werden **alle neuen** Backups automatisch verschlüsselt — sichtbar unter **Einstellungen** als „🔒 Aktiv".

**Wichtige Hinweise:**
- Der Schlüssel wird **ausschließlich** aus der Umgebungsvariable gelesen, nie in der Datenbank gespeichert.
- Geht der Schlüssel verloren, sind die damit verschlüsselten Backups **unwiderruflich nicht mehr entschlüsselbar**.
- Den Schlüssel **getrennt** vom Backup-Speicher sichern (z. B. Passwort-Manager, Bitwarden, Vaultwarden).
- Bereits vorhandene unverschlüsselte Backups bleiben unverschlüsselt — sie werden beim nächsten Backup-Lauf des jeweiligen Containers erneut gesichert und dann verschlüsselt.
- Beim Wiederherstellen entschlüsselt die App automatisch in ein temporäres Verzeichnis — auf der Platte liegt immer nur die verschlüsselte Version.
- Das direkte Volume-Streaming **umgeht** die Verschlüsselung (greift nur für lokal geschriebene Dateien).

---

## Sicherheit & Benutzerverwaltung

### Login & Brute-Force-Schutz

- Alle Seiten der App sind loginpflichtig (Session-Cookie mit bcrypt-gehashten Passwörtern).
- Nach **5 fehlgeschlagenen Login-Versuchen** wird der Account für **5 Minuten gesperrt**.
- Konfigurierbar per Umgebungsvariable:
  - `DBM_LOGIN_MAX_ATTEMPTS` (Standard: `5`)
  - `DBM_LOGIN_LOCKOUT_SECONDS` (Standard: `300` = 5 Minuten)

### Session-Timeout

Die Login-Session läuft standardmäßig nach **7 Tagen** ab. Das bedeutet: nach 7 Tagen ohne Aktivität (oder nach einem Browser-Neustart je nach Cookie-Einstellung) wird man automatisch ausgeloggt.

Der Timeout ist konfigurierbar:
```
DBM_SESSION_MAX_AGE=604800   # Sekunden (Standard: 7 Tage = 604800)
```

Die aktuelle Einstellung (in Stunden) wird unter **Einstellungen → Sitzung & Sicherheit** angezeigt.

### Mehrere Benutzer anlegen (Benutzerverwaltung)

In größeren Umgebungen (Team, Familien-NAS, Unternehmen) können mehrere Benutzer angelegt werden. Es gibt zwei Rollen:

| Rolle | Berechtigungen |
|---|---|
| **Admin** | Vollzugriff: Backups, Restore, Zeitpläne, Einstellungen, Benutzerverwaltung |
| **Nutzer** | Backups, Restore, Zeitpläne — keine Einstellungen und keine Benutzerverwaltung |

**Benutzerverwaltung (nur für Admins):**

Unter **Einstellungen → Benutzerverwaltung** (nur sichtbar für Admins):

- **Tabelle** mit allen Benutzern: Name, Rolle, Erstellt am, Status (Aktiv / Gesperrt)
- **„Neuen Benutzer anlegen"**: Benutzernamen, Passwort und Rolle (Admin/Nutzer) eingeben
- **Löschen**: Benutzer entfernen (eigenen Account und letzten Admin kann man nicht löschen)
- **Entsperren**: Gesperrte Accounts (nach zu vielen Fehlversuchen) manuell freischalten

**Upgrade-Verhalten:** Wer von einer Einzelbenutzer-Installation auf diese Version aktualisiert, bekommt den bestehenden Benutzer automatisch zum Admin — niemand verliert den Zugang.

### Passwort zurücksetzen

Passwort vergessen oder Admin ausgesperrt? Direkt im laufenden Container zurücksetzen:

```bash
docker exec -it docker-backup-manager python -m app.reset_password <benutzername> <neues-passwort>
```

Der Benutzer wird bei Bedarf auch neu angelegt, falls er nicht existiert.

### Betrieb hinter einem Reverse-Proxy

Wird die Web-UI über HTTPS (Traefik, Caddy, nginx, …) bereitgestellt:

```
DBM_SESSION_HTTPS_ONLY=true
```

Das Session-Cookie wird dann nur noch über verschlüsselte Verbindungen übertragen.

> **Sicherheitshinweis:** Der Container benötigt Zugriff auf den Docker-Socket — das entspricht faktisch Root-Rechten auf dem Host. Die Web-UI nicht ungeschützt ins öffentliche Internet stellen. Entweder per VPN absichern oder hinter einem Reverse-Proxy mit TLS und ggf. zusätzlicher IP-Beschränkung betreiben.

---

## Kopplung mit NetPulse (Monitoring)

Optional: Läuft im Heimnetz [NetPulse](https://github.com/Sirbuschi2003/NetPulse), greifen beide Programme ineinander.

- **Keine Fehlalarme während Backups:** Bevor Container gestoppt werden („Container stoppen“), pausiert NetPulse die Überwachung der zugehörigen Dienste und Geräte – kein Alarm, kein Ausfall im Verlauf, kein Abzug bei der Verfügbarkeit. Nach dem Neustart überwacht NetPulse nach einer Nachlaufzeit (Standard 180 s) wieder. Bei Gruppen-Backups (Landscape) wird die ganze Gruppe für die Dauer des Laufs pausiert, bei Wiederherstellungen der wiederhergestellte Container.
- **Sicherheitsnetz:** Jede Pause endet spätestens nach der eingestellten Höchstdauer (Standard 240 min) – auch wenn der Backup Manager abstürzt.
- **Backup-Ergebnisse** (Erfolg/Fehler, Größe, Dauer) gehen an NetPulse; dort gibt es Alarme „Backup fehlgeschlagen“ und „Backup überfällig“.
- **Containerliste** (Name, Compose-Projekt, veröffentlichte Ports, eigene IPs, ob per Zeitplan gesichert) wird stündlich übertragen – NetPulse ordnet daraus Dienste und Geräte zu.

Einrichten: In NetPulse unter *Verbundene Programme → Programm verbinden* einen Schlüssel (`npi_…`) erzeugen, dann hier unter *Einstellungen → NetPulse-Überwachung* Adresse (z. B. `http://192.168.1.10:18081`) und Schlüssel eintragen, einschalten und *Verbindung testen*. Ist NetPulse nicht erreichbar, laufen Backups ganz normal weiter (Hinweis im Log, Kategorie `netpulse`).

## Logs

Unter **Logs** in der Seitenleiste gibt es eine chronologische Ereignishistorie aller Backup-, Restore- und Zeitplan-Läufe mit Zeitstempel, Ergebnis (Erfolg / Fehler / Abbruch) und Fehlerdetails.

Diese Übersicht bleibt dauerhaft in der Datenbank erhalten — anders als die Fortschrittsanzeige unten links, die nur laufende Jobs zeigt und nach einem Neustart der App weg ist.

---

## Umgebungsvariablen – Referenz

| Variable | Standard | Beschreibung |
|---|---|---|
| `DBM_TZ` | `UTC` | IANA-Zeitzone für Zeitpläne (z. B. `Europe/Berlin`) |
| `DBM_SECRET_KEY` | auto | Session-Cookie-Schlüssel. Wird automatisch erzeugt und als `/data/.secret_key` gespeichert wenn nicht gesetzt |
| `DBM_SESSION_MAX_AGE` | `604800` | Session-Laufzeit in Sekunden (Standard: 7 Tage) |
| `DBM_SESSION_HTTPS_ONLY` | `false` | `true` = Session-Cookie nur über HTTPS (für Betrieb hinter Reverse-Proxy) |
| `DBM_ENCRYPTION_KEY` | — | AES-256-Verschlüsselungsschlüssel (Base64, 32 Bytes). Leer = keine Verschlüsselung |
| `DBM_LOGIN_MAX_ATTEMPTS` | `5` | Fehlversuche bis zur Account-Sperre |
| `DBM_LOGIN_LOCKOUT_SECONDS` | `300` | Sperrdauer in Sekunden (Standard: 5 Minuten) |
| `DBM_BASE_DIR` | `/data` | Basisverzeichnis für Datenbank und Backups |
| `DBM_BACKUPS_DIR` | `/data/backups` | Speicherort für Backup-Dateien (überschreibt `DBM_BASE_DIR/backups`) |
| `DBM_DB_PATH` | `/data/dbm.sqlite3` | Pfad zur SQLite-Datenbank |
| `DBM_DEFAULT_RETENTION_COUNT` | `7` | Standard-Anzahl gehaltener Versionen für neue Zeitpläne |
| `DBM_DEFAULT_RETENTION_DAYS` | `0` | Standard-Altersgrenze in Tagen für neue Zeitpläne (0 = deaktiviert) |
| `DBM_HELPER_IMAGE` | `alpine:3.20` | Docker-Image für den Backup-Hilfscontainer |
| `DBM_GZIP_LEVEL` | `6` | gzip-Stufe für lokale Volume-Archive (1 = am schnellsten, 9 = am kleinsten). `1` ist meist 2–3× schneller bei etwas größeren Archiven |
| `DBM_SYNC_PARALLELISM` | `3` | Wie viele Speicherziele nach einem Backup gleichzeitig befüllt werden |
| `DBM_TRANSFER_TIMEOUT` | `43200` | Maximale Dauer (Sekunden) einer einzelnen rclone-Übertragung |
| `DBM_PUBLIC_URL` | — | Externe URL der App (z. B. `http://192.168.1.10:8420`), benötigt für OAuth-Rücksprungadressen. Kann alternativ im Einrichtungs-Assistenten gesetzt werden. |
| `DBM_GOOGLE_CLIENT_ID` | — | Google OAuth Client-ID (für Google Drive). Kann alternativ im Einrichtungs-Assistenten gesetzt werden. |
| `DBM_GOOGLE_CLIENT_SECRET` | — | Google OAuth Client-Secret (für Google Drive). Kann alternativ im Einrichtungs-Assistenten gesetzt werden. |
| `DBM_MS_CLIENT_ID` | — | Microsoft Azure App-ID (für OneDrive). Kann alternativ im Einrichtungs-Assistenten gesetzt werden. |
| `DBM_MS_CLIENT_SECRET` | — | Microsoft Client-Secret (für OneDrive). Kann alternativ im Einrichtungs-Assistenten gesetzt werden. |
| `DBM_MS_TENANT` | `common` | Azure-Tenant (Standard `common` = private + Firmenkonten) |

---

## Entwicklung & Tests

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

Die Test-Suite deckt Retention-Regeln, Namens-Sanitizing, Restore-Config-Mapping, Job-Fortschritt, Storage-Sync und einen kompletten App-Boot-/Login-Smoketest ab. Docker-abhängige Funktionen (Backup/Restore realer Container) werden über die App selbst manuell getestet.

```bash
# Einzelnen Test-Modul ausführen
pytest tests/test_retention.py -v

# App direkt starten (ohne Docker)
DBM_BASE_DIR=./data uvicorn app.main:app --reload --port 8420
```

**Projektstruktur:**

```
app/
├── main.py              FastAPI-App, Middleware, Router-Registrierung
├── config.py            Konfiguration via Umgebungsvariablen
├── models.py            SQLAlchemy-Modelle (BackupRecord, Schedule, User, …)
├── database.py          DB-Initialisierung und Schema-Migration
├── auth.py              Authentifizierungs-Abhängigkeiten (get_current_user, get_admin_user)
├── backup_engine.py     Container-Backup-Logik, Hilfscontainer, tar-Archivierung
├── restore_engine.py    Container-Wiederherstellungs-Logik
├── restic_engine.py     Volume-Streaming via restic + rclone
├── scheduler.py         APScheduler-Integration, Aufbewahrungsrichtlinie
├── storage_sync.py      Speicherziel-Synchronisierung (SMB, S3, rclone, …)
├── rclone_backends.py   SFTP, WebDAV, FTP, Azure, Dropbox, weitere Clouds (rclone-Konfiguration aus der Weboberfläche)
├── encryption.py        AES-256-CBC-Verschlüsselung
├── job_tracker.py       Laufende Job-Fortschrittsverfolgung
├── event_log.py         Persistente Ereignishistorie
├── routers/
│   ├── auth.py          Login, Logout, Benutzerverwaltung
│   ├── backups.py       Backup-CRUD, Löschen mit Offsite-Bereinigung
│   ├── containers.py    Container-Liste, Backup auslösen
│   ├── schedules.py     Zeitplan-CRUD
│   ├── settings.py      Speicherziele, OAuth, Einstellungen-Übersicht
│   ├── jobs.py          Job-Fortschritt (SSE)
│   └── logs.py          Ereignislog-Abfrage
└── static/
    ├── index.html       Single-Page-App Shell
    ├── img/             BuscheHome-Logo und Favicons (siehe NOTICE)
    └── js/app.js        Gesamtes Frontend (vanilla JS, kein Build-Schritt)
```

---

## Autor, Marke & Lizenzhinweis

Docker Backup Manager wird von **Thomas Busche** unter dem Namen **BuscheHome** entwickelt.

Der Name „BuscheHome“ und das BuscheHome-Logo (Dateien unter `app/static/img/`) sind Marken bzw. Kennzeichen des Autors. Sie sind **nicht** Teil der Open-Source-Lizenz des Quellcodes: Den Code darfst du im Rahmen der Lizenz nutzen, ändern und weitergeben, Name und Logo aber nicht für eigene Projekte, Forks oder Produkte verwenden oder so einsetzen, dass der Eindruck entsteht, sie stammten von BuscheHome oder seien von BuscheHome unterstützt. Wer einen veränderten Fork veröffentlicht, entfernt bitte Logo und Namen bzw. ersetzt sie durch eigene. Ein sachlicher Hinweis auf die Herkunft („basiert auf Docker Backup Manager von BuscheHome“) ist ausdrücklich erwünscht.

Details stehen in der Datei [NOTICE](NOTICE).
