"""
Optionale Kopplung mit NetPulse (Netzwerk-Monitoring, https://github.com/Sirbuschi2003/NetPulse).

Ist sie eingeschaltet, greifen beide Programme wie Zahnräder ineinander:

- **Pause**: Bevor ein Container für ein Backup (oder eine Wiederherstellung) gestoppt wird, bittet der
  Backup Manager NetPulse, die zugehörigen Dienste/Geräte nicht zu prüfen. Nach dem Neustart meldet er
  „fertig“ - NetPulse wartet noch eine Nachlaufzeit (Hochfahren) und überwacht dann wieder. So entstehen
  keine Fehlalarme und keine Ausfälle im Verlauf. Jede Pause hat ein spätestes Ende: Stürzt der Backup
  Manager ab, überwacht NetPulse danach von selbst weiter.
- **Ergebnisse**: Erfolg/Fehlschlag jedes Container-Backups geht an NetPulse (dort gibt es Alarm-Regeln
  „Backup fehlgeschlagen“ und „Backup überfällig“).
- **Inventar**: Die Containerliste (Name, Compose-Projekt, veröffentlichte Ports, eigene IPs, ob regelmäßig
  gesichert) geht stündlich an NetPulse, damit man dort die Zuordnung vorab sieht und anpassen kann.

Alles ist „best effort“: Ist NetPulse nicht erreichbar, läuft das Backup ganz normal weiter (mit einem
Hinweis im Log). Der Schlüssel von NetPulse erlaubt nur diese Aufrufe.
"""
from __future__ import annotations

import datetime
from app.clock import utcnow
import json
import logging
import ssl
import threading
import urllib.error
import urllib.request
from typing import Optional

from app.config import APP_VERSION
from app.database import SessionLocal
from app.models import AppSetting

logger = logging.getLogger("dbm.netpulse")

SETTING_KEY = "netpulse"
DEFAULTS = {
    "enabled": False,
    "url": "",
    "token": "",
    # Eigenes Zertifikat von NetPulse (z. B. https://192.168.1.10:8443) - dann Prüfung abschalten
    "verify_tls": True,
    # Überwachung pausieren, solange Container für Backup/Restore gestoppt sind
    "pause": True,
    # Backup-Ergebnisse melden
    "report": True,
    # Nachlaufzeit nach dem Neustart, bis NetPulse wieder prüft
    "grace_s": 180,
    # Spätestes Ende einer Pause (falls der Backup Manager sich nicht zurückmeldet)
    "max_minutes": 240,
}
TIMEOUT_S = 6

_status_lock = threading.Lock()
_status: dict = {"last_ok_at": None, "last_error": None, "last_error_at": None}


class NetPulseError(Exception):
    pass


# ---------- Einstellungen ----------

def get_config() -> dict:
    db = SessionLocal()
    try:
        row = db.get(AppSetting, SETTING_KEY)
        stored = json.loads(row.value) if row and row.value else {}
    except Exception:  # noqa: BLE001
        stored = {}
    finally:
        db.close()
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


def save_config(cfg: dict) -> dict:
    clean = {**DEFAULTS, **{k: v for k, v in cfg.items() if k in DEFAULTS}}
    clean["url"] = str(clean["url"]).strip().rstrip("/")
    clean["token"] = str(clean["token"]).strip()
    clean["grace_s"] = max(0, min(1800, int(clean["grace_s"] or 0)))
    clean["max_minutes"] = max(10, min(720, int(clean["max_minutes"] or 240)))
    for k in ("enabled", "verify_tls", "pause", "report"):
        clean[k] = bool(clean[k])
    db = SessionLocal()
    try:
        row = db.get(AppSetting, SETTING_KEY)
        if row is None:
            row = AppSetting(key=SETTING_KEY)
            db.add(row)
        row.value = json.dumps(clean)
        db.commit()
    finally:
        db.close()
    return clean


def public_config(cfg: Optional[dict] = None) -> dict:
    """Für die Oberfläche - ohne den Schlüssel."""
    cfg = cfg or get_config()
    out = {k: v for k, v in cfg.items() if k != "token"}
    out["token_set"] = bool(cfg.get("token"))
    with _status_lock:
        out["status"] = dict(_status)
    return out


def _active(cfg: dict) -> bool:
    return bool(cfg.get("enabled") and cfg.get("url") and cfg.get("token"))


# ---------- HTTP ----------

def _request(cfg: dict, method: str, path: str, body: Optional[dict] = None) -> dict:
    url = cfg["url"].rstrip("/") + "/api/integration/v1" + path
    if not url.startswith(("http://", "https://")):
        raise NetPulseError("Adresse muss mit http:// oder https:// beginnen")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {cfg['token']}",
        "Content-Type": "application/json",
        "User-Agent": f"docker-backup-manager/{APP_VERSION}",
    })
    context = None
    if url.startswith("https://") and not cfg.get("verify_tls", True):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=context) as resp:
            raw = resp.read()
        result = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error", "")
        except Exception:  # noqa: BLE001
            detail = ""
        if exc.code == 401:
            msg = "Schlüssel ungültig oder Verbindung in NetPulse ausgeschaltet"
        elif exc.code == 404:
            msg = "Schnittstelle nicht gefunden - ist das wirklich NetPulse (ab Version mit „Verbundene Programme“)?"
        else:
            msg = f"HTTP {exc.code}{': ' + detail if detail else ''}"
        _set_error(msg)
        raise NetPulseError(msg) from exc
    except ssl.SSLCertVerificationError as exc:
        msg = "Zertifikat nicht vertrauenswürdig - bei eigenem Zertifikat „Zertifikat prüfen“ ausschalten"
        _set_error(msg)
        raise NetPulseError(msg) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        msg = f"nicht erreichbar ({reason})"
        _set_error(msg)
        raise NetPulseError(msg) from exc
    with _status_lock:
        _status["last_ok_at"] = utcnow().isoformat() + "Z"
        _status["last_error"] = None
    return result


def _set_error(msg: str) -> None:
    with _status_lock:
        _status["last_error"] = msg
        _status["last_error_at"] = utcnow().isoformat() + "Z"


# ---------- Inventar ----------

_macvlan_cache: dict[str, bool] = {}


def _own_ip_network(client, net_name: str) -> bool:
    """macvlan/ipvlan: Der Container hat eine eigene Adresse im Heimnetz (ist dort ein eigenes Gerät)."""
    if net_name not in _macvlan_cache:
        try:
            driver = client.networks.get(net_name).attrs.get("Driver", "")
        except Exception:  # noqa: BLE001
            driver = ""
        _macvlan_cache[net_name] = driver in ("macvlan", "ipvlan")
    return _macvlan_cache[net_name]


def _scheduled_names() -> set[str]:
    """Container, die ein aktiver Zeitplan regelmäßig sichert."""
    from app import backup_engine
    from app.models import Schedule

    names: set[str] = set()
    db = SessionLocal()
    try:
        for s in db.query(Schedule).filter(Schedule.enabled.is_(True)).all():
            if s.target_type == "container" and s.target_ref:
                names.add(s.target_ref)
            elif s.target_type == "landscape":
                try:
                    names.update(c.name for c in backup_engine.list_landscape_containers(
                        s.project_filter, s.name_contains, s.exclude_names))
                except Exception:  # noqa: BLE001
                    pass
    finally:
        db.close()
    return names


def inventory() -> list[dict]:
    from app.docker_client import get_client

    client = get_client()
    scheduled = _scheduled_names()
    out = []
    for c in client.containers.list(all=True):
        attrs = c.attrs
        ports = set()
        for bindings in (attrs.get("NetworkSettings", {}).get("Ports") or {}).values():
            for b in bindings or []:
                try:
                    ports.add(int(b.get("HostPort")))
                except (TypeError, ValueError):
                    pass
        ips = []
        for net_name, net in (attrs.get("NetworkSettings", {}).get("Networks") or {}).items():
            ip = net.get("IPAddress")
            if ip and _own_ip_network(client, net_name):
                ips.append(ip)
        if attrs.get("HostConfig", {}).get("NetworkMode") == "host":
            # Netzwerk des Hosts: Dienste laufen direkt auf dessen Ports
            for p in (attrs.get("Config", {}).get("ExposedPorts") or {}):
                try:
                    ports.add(int(p.split("/")[0]))
                except ValueError:
                    pass
        out.append({
            "name": c.name,
            "project": c.labels.get("com.docker.compose.project"),
            "image": (attrs.get("Config", {}) or {}).get("Image"),
            "ports": sorted(ports),
            "ips": ips,
            "running": attrs.get("State", {}).get("Status") == "running",
            "scheduled": c.name in scheduled,
        })
    return out


def push_inventory(cfg: Optional[dict] = None) -> Optional[dict]:
    cfg = cfg or get_config()
    if not _active(cfg):
        return None
    try:
        return _request(cfg, "PUT", "/inventory", {"subjects": inventory()})
    except NetPulseError as exc:
        logger.warning("NetPulse: Containerliste nicht übertragen - %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("NetPulse: Containerliste nicht ermittelt - %s", exc)
    return None


def push_inventory_async() -> None:
    threading.Thread(target=push_inventory, daemon=True, name="netpulse-inventory").start()


def test(cfg: dict) -> dict:
    """Verbindung prüfen und Containerliste übertragen - Fehler werden als NetPulseError gemeldet."""
    if not cfg.get("url") or not cfg.get("token"):
        raise NetPulseError("Bitte Adresse und Schlüssel eintragen")
    hello = _request(cfg, "GET", "/hello")
    result = _request(cfg, "PUT", "/inventory", {"subjects": inventory()})
    return {"hello": hello, "subjects": result.get("subjects", [])}


# ---------- Pause & Ergebnis ----------

def pause(names: list[str], reason: str = "Backup", minutes: Optional[int] = None) -> Optional[int]:
    """Überwachung pausieren; liefert die Pausen-Nummer (oder None, wenn aus/nicht erreichbar)."""
    cfg = get_config()
    if not _active(cfg) or not cfg.get("pause") or not names:
        return None
    try:
        res = _request(cfg, "POST", "/pause", {
            "subjects": names, "reason": reason, "minutes": minutes or cfg.get("max_minutes", 240),
        })
        matched = res.get("matches") or []
        logger.info("NetPulse: Überwachung pausiert für %s (%s)", ", ".join(names),
                    ", ".join(m.get("label", "?") for m in matched) or "nichts zugeordnet")
        return res.get("id")
    except NetPulseError as exc:
        logger.warning("NetPulse: Pause für %s nicht möglich - %s (Backup läuft trotzdem)", ", ".join(names), exc)
        return None


def resume(pause_id: Optional[int]) -> None:
    if not pause_id:
        return
    cfg = get_config()
    if not _active(cfg):
        return
    try:
        _request(cfg, "POST", f"/pause/{pause_id}/end", {"grace_s": cfg.get("grace_s", 180)})
    except NetPulseError as exc:
        logger.warning("NetPulse: Pause %s nicht beendet - %s (endet von selbst nach der Höchstdauer)", pause_id, exc)


def report(subject: str, status: str, message: Optional[str] = None, size_bytes: Optional[int] = None,
           duration_s: Optional[float] = None, kind: str = "backup") -> None:
    cfg = get_config()
    if not _active(cfg) or not cfg.get("report"):
        return
    try:
        _request(cfg, "POST", "/report", {
            "kind": kind, "subject": subject, "status": status, "message": message,
            "size_bytes": size_bytes, "duration_s": duration_s,
        })
    except NetPulseError as exc:
        logger.warning("NetPulse: Ergebnis für %s nicht gemeldet - %s", subject, exc)
