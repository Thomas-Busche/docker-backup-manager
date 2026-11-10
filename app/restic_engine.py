"""
Inkrementelle, deduplizierte und verschlüsselte Volume-Sicherung mit restic.

Wird eingesetzt wenn bei einem Backup ein stream_target (stream_volumes_target_id)
konfiguriert ist – ersetzt das bisherige rohe Tar-Streaming durch ein restic-Repo
auf dem Ziel, das deduplizierung, Inkrementalität und eingebaute Verschlüsselung
liefert.

Restic-Repo-Layout pro Container:
  <target_root>/<container_name>/restic_repo/
      config
      data/
      index/
      keys/
      locks/
      snapshots/

Jedes Volume-/Bind-Backup erzeugt einen restic-Snapshot mit einem eindeutigen
Tag (z. B. "nextcloud/nextcloud_data"). Der Snapshot enthält eine einzige Datei
"data.tar" – den ungepackten Tar-Stream des Volumes. restic selbst übernimmt
Content-Defined Chunking, Dedup und AES-256-Verschlüsselung.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

from app.config import BASE_DIR

logger = logging.getLogger("dbm.restic_engine")

RESTIC_BIN = "restic"


def _fmtb(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# Availability + password
# ---------------------------------------------------------------------------

def is_available() -> bool:
    """True wenn restic-Binary im PATH gefunden wurde."""
    return shutil.which(RESTIC_BIN) is not None


def get_password() -> str:
    """
    Restic-Repo-Passwort.

    Reihenfolge:
    1. DBM_ENCRYPTION_KEY   – bestehender At-Rest-Key wird als Restic-PW mitgenutzt
    2. DBM_RESTIC_PASSWORD  – eigener Restic-Key wenn At-Rest-Verschlüsselung getrennt gehalten werden soll
    3. Auto-generiert       – einmalig erzeugt, persistiert in /data/.restic_password

    So sind Restic-Repos auch ohne explizite Konfiguration sofort verschlüsselt.
    """
    pw = os.environ.get("DBM_ENCRYPTION_KEY") or os.environ.get("DBM_RESTIC_PASSWORD")
    if pw:
        return pw
    pw_path = BASE_DIR / ".restic_password"
    if pw_path.exists():
        return pw_path.read_text().strip()
    pw = secrets.token_hex(32)
    pw_path.write_text(pw)
    try:
        os.chmod(pw_path, 0o600)
    except OSError:
        pass
    logger.info("Restic-Passwort automatisch generiert und in %s gespeichert", pw_path)
    return pw


# ---------------------------------------------------------------------------
# rclone config helper for SMB backend
# ---------------------------------------------------------------------------

def _obscure_rclone_password(plain: str) -> str:
    """Gibt das rclone-obfuskierte Passwort zurück (benötigt für rclone.conf)."""
    result = subprocess.run(
        ["rclone", "obscure", plain], capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        raise RuntimeError(f"rclone obscure fehlgeschlagen: {result.stderr.strip()}")
    return result.stdout.strip()


def _write_smb_rclone_conf(config: dict) -> str:
    """
    Schreibt eine temporäre rclone.conf für einen SMB-Target und gibt den Pfad zurück.
    Der Aufrufer muss die Datei nach Abschluss des restic-Prozesses löschen.
    """
    obscured_pw = _obscure_rclone_password(config["password"])
    domain = config.get("domain", "").strip()
    port = int(config.get("port") or 445)
    lines = [
        "[dbm_smb]",
        "type = smb",
        f"host = {config['server']}",
        f"port = {port}",
        f"user = {config['username']}",
        f"pass = {obscured_pw}",
    ]
    if domain:
        lines.append(f"domain = {domain}")
    # Close idle TCP connections quickly so a stale/dropped SMB connection
    # doesn't silently block rclone forever.
    lines.append("idle_timeout = 1m")
    conf_text = "\n".join(lines) + "\n"
    tmp_dir = BASE_DIR / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"rclone_restic_{secrets.token_hex(8)}.conf"
    tmp_path.write_text(conf_text)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    return str(tmp_path)


# ---------------------------------------------------------------------------
# Repo URL + environment
# ---------------------------------------------------------------------------

def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def repo_url_and_env(
    target_type: str, config: dict, container_name: str
) -> tuple[str, dict, Optional[str]]:
    """
    Gibt (repo_url, env_dict, smb_conf_path_oder_None) zurück.

    smb_conf_path ist eine temporäre rclone.conf die nach dem restic-Aufruf
    gelöscht werden muss; für alle anderen Target-Typen ist es None.

    Unterstützte Typen: local_path, smb, s3, rclone
    """
    safe = _safe(container_name)
    env: dict = {}
    smb_conf_path: Optional[str] = None

    if target_type == "local_path":
        repo = str(Path(config["path"]) / safe / "restic_repo")

    elif target_type == "smb":
        # Users sometimes type Windows-style paths ("Backup\\Clients"); rclone needs "/".
        share = config["share"].replace("\\", "/").strip("/")
        base = config.get("base_path", "").replace("\\", "/").strip("/")
        parts = [p for p in [share, base, safe, "restic_repo"] if p]
        repo = "rclone:dbm_smb:" + "/".join(parts)
        smb_conf_path = _write_smb_rclone_conf(config)
        env["RCLONE_CONFIG"] = smb_conf_path

    elif target_type == "s3":
        endpoint = config.get("endpoint_url", "").strip().rstrip("/")
        bucket = config["bucket"]
        prefix = config.get("prefix", "").strip("/")
        base_parts = [p for p in [prefix, safe, "restic_repo"] if p]
        if endpoint:
            repo = f"s3:{endpoint}/{bucket}/" + "/".join(base_parts)
        else:
            region = config.get("region", "us-east-1") or "us-east-1"
            repo = f"s3:s3.amazonaws.com/{bucket}/" + "/".join(base_parts)
            env["AWS_DEFAULT_REGION"] = region
        env["AWS_ACCESS_KEY_ID"] = config["access_key"]
        env["AWS_SECRET_ACCESS_KEY"] = config["secret_key"]

    elif target_type == "rclone":
        remote = config["remote"]
        remote_path = config.get("remote_path", "").strip("/")
        base_parts = [p for p in [remote_path, safe, "restic_repo"] if p]
        repo = f"rclone:{remote}:" + "/".join(base_parts)
        # restic spawns rclone itself; without this it looks for
        # ~/.config/rclone/rclone.conf and can't find the configured remote.
        from app.storage_sync import RCLONE_CONFIG_PATH
        env["RCLONE_CONFIG"] = RCLONE_CONFIG_PATH

    else:
        raise ValueError(
            f"Restic unterstützt diesen Zieltyp nicht: '{target_type}' "
            "(unterstützt: local_path, smb, s3, rclone)"
        )

    return repo, env, smb_conf_path


def _full_env(env: dict, password: str) -> dict:
    return {**os.environ, **env, "RESTIC_PASSWORD": password}


# ---------------------------------------------------------------------------
# Network upload monitor (reads /proc/net/dev TX bytes from container)
# ---------------------------------------------------------------------------

def _net_tx_bytes() -> Optional[int]:
    """Total TX bytes across all non-loopback interfaces from /proc/net/dev."""
    try:
        with open("/proc/net/dev") as f:
            total = 0
            for line in f.readlines()[2:]:
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                if parts[0].strip() == "lo":
                    continue
                fields = parts[1].split()
                if len(fields) >= 9:
                    total += int(fields[8])  # TX bytes column
            return total
    except Exception:
        return None


def _start_upload_monitor(on_upload_bytes: Callable, stop_event: threading.Event) -> threading.Thread:
    """Starts a background thread sampling /proc/net/dev every second."""
    def _monitor():
        prev = _net_tx_bytes()
        while not stop_event.wait(1.0):
            curr = _net_tx_bytes()
            if prev is not None and curr is not None:
                delta = curr - prev
                if delta > 0:
                    on_upload_bytes(delta)
            prev = curr

    t = threading.Thread(target=_monitor, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Core restic operations
# ---------------------------------------------------------------------------

def init_repo_if_needed(repo_url: str, env: dict, password: str,
                         on_log: Optional[Callable] = None) -> None:
    """Initialisiert das Restic-Repo wenn es noch nicht existiert."""
    def _log(msg):
        if on_log:
            on_log(msg)

    _log("Verbinde mit Restic-Repo …")
    result = subprocess.run(
        [RESTIC_BIN, "-r", repo_url, "snapshots", "--json", "--no-lock"],
        capture_output=True,
        env=_full_env(env, password),
        timeout=60,
    )
    if result.returncode == 0:
        _log("Repo vorhanden, verwende vorhandenes Repo (inkrementell)")
        return
    _log("Repo nicht gefunden, lege neues Repo an …")
    result = subprocess.run(
        [RESTIC_BIN, "-r", repo_url, "init"],
        capture_output=True,
        env=_full_env(env, password),
        timeout=120,
    )
    if result.returncode != 0:
        err = result.stderr.decode(errors='replace').strip()
        _log(f"FEHLER restic init: {err}")
        raise RuntimeError(f"restic init fehlgeschlagen: {err}")
    logger.info("Restic-Repo angelegt: %s", repo_url)
    _log("Neues Restic-Repo angelegt")


# Seconds without any stdout output before the restic process is considered
# hung and gets killed.  30 minutes is conservative enough for large volumes
# on slow NAS links while still catching a silently stalled SMB connection.
_RESTIC_HANG_TIMEOUT = 30 * 60


def backup_volume_from_stream(
    chunks: Iterator[bytes],
    repo_url: str,
    env: dict,
    password: str,
    tag: str,
    on_bytes=None,
    should_cancel=None,
    on_upload_bytes: Optional[Callable] = None,
    on_log: Optional[Callable] = None,
) -> tuple[str, int]:
    """
    Streamt einen unkomprimierten Tar eines Volumes in ein Restic-Repo.
    Gibt (snapshot_id, total_bytes_processed) zurück — total_bytes_processed
    ist die tatsächliche unkomprimierte Datenmenge des Volumes (aus restic summary).

    chunks          – Iterator von Bytes (unkomprimierter Tar-Stream, kein gzip)
    tag             – wird am Snapshot gespeichert, z. B. "nextcloud/nextcloud_data"
    on_bytes        – Callback für Leserate (bytes aus Docker-Volume gelesen)
    on_upload_bytes – Callback für Uploadrate (bytes tatsächlich übers Netz übertragen)
    on_log          – Callback für Live-Log-Nachrichten in der UI
    """
    def _log(msg):
        if on_log:
            on_log(msg)

    proc = subprocess.Popen(
        [
            RESTIC_BIN, "-r", repo_url,
            "backup", "--stdin", "--stdin-filename", "data.tar",
            "--tag", tag, "--json",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_full_env(env, password),
    )

    # Read stdout in a background thread so we can (a) collect it while stdin
    # is being written without deadlocking on a full pipe buffer, (b) track
    # the timestamp of the last output for the hang watchdog, and (c) forward
    # restic's JSON status/summary messages to the live log in real-time.
    stdout_lines: list[bytes] = []
    last_output_at: list[float] = [time.monotonic()]

    last_status_bucket: list[int] = [-1]

    def _read_stdout():
        for raw_line in proc.stdout:
            stdout_lines.append(raw_line)
            last_output_at[0] = time.monotonic()
            if on_log:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    mtype = data.get("message_type", "")
                    if mtype == "status":
                        elapsed = int(data.get("seconds_elapsed", 0))
                        bucket = elapsed // 30
                        if bucket > 0 and bucket != last_status_bucket[0]:
                            last_status_bucket[0] = bucket
                            _log(f"restic läuft … {elapsed}s")
                    elif mtype == "summary":
                        added = data.get("data_added_packed", 0)
                        total = data.get("total_bytes_processed", 0)
                        sid = data.get("snapshot_id", "")[:8]
                        _log(
                            f"restic fertig: Snapshot {sid} — "
                            f"gelesen {_fmtb(total)}, neu auf NAS {_fmtb(added)}"
                        )
                    elif mtype == "error":
                        _log(f"restic FEHLER: {data.get('error', line)}")
                except json.JSONDecodeError:
                    if line.startswith("Fatal"):
                        _log(f"restic: {line}")

    stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
    stdout_thread.start()

    stop_monitor = threading.Event()
    monitor_thread = None
    if on_upload_bytes is not None:
        monitor_thread = _start_upload_monitor(on_upload_bytes, stop_monitor)

    _REPORT_INTERVAL = 1024 * 1024  # on_bytes alle 1 MB aufrufen
    pending = 0
    try:
        for chunk in chunks:
            if should_cancel and should_cancel():
                proc.kill()
                raise RuntimeError("Backup abgebrochen")
            proc.stdin.write(chunk)
            if on_bytes:
                pending += len(chunk)
                if pending >= _REPORT_INTERVAL:
                    on_bytes(pending)
                    pending = 0
        proc.stdin.close()
        if on_bytes and pending:
            on_bytes(pending)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    finally:
        stop_monitor.set()
        if monitor_thread is not None:
            monitor_thread.join(timeout=2)

    # Wait for restic to finish.  Poll in 5 s intervals so we can respond to
    # cancellation requests and detect a hung process without blocking forever.
    while True:
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            if should_cancel and should_cancel():
                _log("Backup wird abgebrochen — restic wird beendet …")
                proc.kill()
                raise RuntimeError("Backup abgebrochen")
            idle = time.monotonic() - last_output_at[0]
            if idle > _RESTIC_HANG_TIMEOUT:
                _log(
                    f"⚠️ restic hängt seit {int(idle // 60)} Min ohne Ausgabe — "
                    "Prozess wird abgebrochen (SMB-Verbindung unterbrochen?)"
                )
                proc.kill()
                raise RuntimeError(
                    f"restic backup hängt: kein Fortschritt seit {int(idle // 60)} Minuten — "
                    "Prozess abgebrochen. Mögliche Ursache: SMB-Verbindung zum NAS unterbrochen."
                )
            if idle > 60 and int(idle) % 60 < 5:
                _log(f"restic schreibt auf NAS … (letzte Aktivität vor {int(idle)}s)")
            logger.debug("restic läuft noch, letzte Ausgabe vor %.0fs", idle)

    stdout_thread.join(timeout=5)
    stderr = proc.stderr.read()
    code = proc.returncode

    if code != 0:
        raise RuntimeError(
            f"restic backup fehlgeschlagen: {stderr.decode(errors='replace').strip()}"
        )

    for raw in stdout_lines:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get("message_type") == "summary":
                sid = data.get("snapshot_id", "")
                if sid:
                    logger.info("Restic-Snapshot %s erstellt (tag=%s)", sid[:8], tag)
                    # total_bytes_processed = full uncompressed source size read by restic.
                    # This reflects the actual data volume being protected, not just the
                    # incremental delta (data_added_packed), which shrinks to near-zero for
                    # subsequent backups of mostly-unchanged data (e.g. a photo library).
                    return sid, data.get("total_bytes_processed", data.get("data_added_packed", 0))
        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        "Restic-Snapshot-ID nicht in Ausgabe gefunden: "
        + b"".join(stdout_lines).decode(errors="replace")[:500]
    )


def _delete_repo_folder(repo_url: str, env: dict) -> None:
    """Löscht den gesamten Restic-Repo-Ordner auf dem Speicherziel.
    Wird aufgerufen wenn das Repo leer ist (keine Snapshots mehr übrig).
    Wirft RuntimeError bei echten Fehlern; gibt None zurück wenn der Ordner
    nicht existiert (kein Fehler — schon weg)."""
    if repo_url.startswith("rclone:"):
        # "rclone:remote:path" → rclone erwartet "remote:path"
        rclone_target = repo_url[len("rclone:"):]
        result = subprocess.run(
            ["rclone", "purge", rclone_target],
            capture_output=True,
            env={**os.environ, **env},
            timeout=120,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            # rclone exits with non-zero when the path doesn't exist — that's OK
            if "directory not found" in stderr.lower() or "object not found" in stderr.lower() or "not found" in stderr.lower():
                logger.info("Restic-Repo-Ordner nicht vorhanden (bereits gelöscht): %s", rclone_target)
                return
            raise RuntimeError(f"rclone purge fehlgeschlagen: {stderr}")
        logger.info("Restic-Repo-Ordner gelöscht: %s", rclone_target)
    elif not repo_url.startswith("s3:"):
        # Lokaler Pfad
        import shutil
        shutil.rmtree(repo_url, ignore_errors=True)
        logger.info("Lokaler Restic-Repo-Ordner gelöscht: %s", repo_url)
    # S3: Bucket-Pfad wird über rclone verwaltet — TODO wenn benötigt


def _repo_is_empty(repo_url: str, env: dict, password: str) -> bool:
    """Gibt True zurück wenn das Repo keine Snapshots mehr enthält."""
    result = subprocess.run(
        [RESTIC_BIN, "-r", repo_url, "snapshots", "--json", "--no-lock"],
        capture_output=True,
        env=_full_env(env, password),
        timeout=60,
    )
    if result.returncode != 0:
        return False
    try:
        return json.loads(result.stdout.decode()) == []
    except Exception:
        return False


def forget_snapshot(repo_url: str, env: dict, password: str, snapshot_id: str) -> None:
    """Löscht einen Restic-Snapshot und bereinigt nicht mehr referenzierte Daten."""
    result = subprocess.run(
        [RESTIC_BIN, "-r", repo_url, "forget", "--prune", snapshot_id],
        capture_output=True,
        env=_full_env(env, password),
        # prune rewrites pack files - on a large repo over a WAN link this
        # takes far longer than a few minutes.
        timeout=3 * 3600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"restic forget fehlgeschlagen: {result.stderr.decode(errors='replace').strip()}"
        )


def dump_snapshot_to_file(
    repo_url: str, env: dict, password: str, snapshot_id: str, dest_path: Path
) -> None:
    """
    Extrahiert data.tar aus einem Restic-Snapshot in eine lokale Datei.
    Die Datei kann anschließend per 'tar xf' in ein Docker-Volume eingespielt werden.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [RESTIC_BIN, "-r", repo_url, "dump", snapshot_id, "data.tar"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_full_env(env, password),
    )
    with open(dest_path, "wb") as f:
        for chunk in iter(lambda: proc.stdout.read(1024 * 1024), b""):
            f.write(chunk)
    stderr = proc.stderr.read()
    code = proc.wait()
    if code != 0:
        raise RuntimeError(
            f"restic dump fehlgeschlagen: {stderr.decode(errors='replace').strip()}"
        )


# ---------------------------------------------------------------------------
# Convenience: snapshot cleanup on backup delete
# ---------------------------------------------------------------------------

def cleanup_restic_repo_for_container(
    container_name: str, stream_target: tuple
) -> None:
    """Prüft ob das Restic-Repo für einen Container leer ist und löscht es ggf.
    Wird für abgebrochene/fehlgeschlagene Backups ohne meta.json verwendet."""
    if not container_name or stream_target is None:
        return
    target_type, target_config_json, _tid = stream_target
    target_config = json.loads(target_config_json or "{}")
    password = get_password()
    repo_url, env, smb_conf_path = repo_url_and_env(target_type, target_config, container_name)
    try:
        if _repo_is_empty(repo_url, env, password):
            logger.info("Restic-Repo für '%s' leer/nicht initialisiert, lösche Ordner", container_name)
            try:
                _delete_repo_folder(repo_url, env)
            except Exception as exc:
                logger.warning("Restic-Repo-Ordner konnte nicht gelöscht werden: %s", exc)
    except Exception as exc:
        logger.warning("Restic-Repo-Cleanup für '%s' fehlgeschlagen: %s", container_name, exc)
    finally:
        if smb_conf_path:
            try:
                Path(smb_conf_path).unlink(missing_ok=True)
            except Exception:
                pass


def purge_restic_repo(container_name: str, stream_target: tuple) -> None:
    """Löscht den gesamten Restic-Repo-Ordner für einen Container — auch wenn
    noch Snapshots darin sind (z.B. orphaned Daten aus fehlgeschlagenen Backups).
    Nur aufrufen wenn keine weiteren Backup-Records für diesen Container existieren.
    Wirft bei echten Fehlern (Verbindungsprobleme, Rechtefehler) eine Exception
    damit der Aufrufer weiß dass Daten möglicherweise noch auf dem Ziel verbleiben."""
    if not container_name or stream_target is None:
        return
    target_type, target_config_json, _tid = stream_target
    target_config = json.loads(target_config_json or "{}")
    repo_url, env, smb_conf_path = repo_url_and_env(target_type, target_config, container_name)
    try:
        _delete_repo_folder(repo_url, env)
        logger.info("Restic-Repo für '%s' vollständig gelöscht", container_name)
    finally:
        if smb_conf_path:
            try:
                Path(smb_conf_path).unlink(missing_ok=True)
            except Exception:
                pass


def maybe_forget_restic(backup_path: Path, stream_target: Optional[tuple]) -> None:
    """
    Liest meta.json und vergisst restic-Snapshots wenn es sich um ein restic-Backup
    handelt. Nach dem Vergessen wird geprüft ob das Repo leer ist und ggf. der
    gesamte Ordner auf dem Speicherziel gelöscht. Best-effort: Fehler werden
    geloggt aber nicht weitergeworfen.

    stream_target – (target_type, config_json, target_id) Tupel aus der DB
    """
    if stream_target is None:
        return
    meta_path = Path(backup_path) / "meta.json"
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return
    if meta.get("backup_engine") != "restic":
        return

    snapshot_ids: dict = meta.get("restic_snapshot_ids", {})
    repo_url: str = meta.get("restic_repo_url", "")
    if not repo_url:
        return

    target_type, target_config_json, _tid = stream_target
    target_config = json.loads(target_config_json or "{}")
    container_name = meta.get("container_name", "")
    password = get_password()
    _, env, smb_conf_path = repo_url_and_env(target_type, target_config, container_name)
    try:
        for key, snapshot_id in snapshot_ids.items():
            try:
                forget_snapshot(repo_url, env, password, snapshot_id)
                logger.info("Restic-Snapshot %s vergessen (%s)", snapshot_id[:8], key)
            except Exception as exc:
                logger.warning(
                    "Restic forget fehlgeschlagen (snapshot=%s, key=%s): %s",
                    snapshot_id[:8], key, exc,
                )
        # Prüfen ob Repo nun leer ist — wenn ja, Ordner komplett löschen
        try:
            if _repo_is_empty(repo_url, env, password):
                logger.info("Restic-Repo leer nach forget, lösche Ordner: %s", repo_url)
                _delete_repo_folder(repo_url, env)
        except Exception as exc:
            logger.warning("Restic-Repo-Ordner-Cleanup fehlgeschlagen: %s", exc)
    finally:
        if smb_conf_path:
            try:
                Path(smb_conf_path).unlink(missing_ok=True)
            except Exception:
                pass
