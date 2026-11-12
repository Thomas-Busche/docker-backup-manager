"""
Storage targets that run on the bundled rclone binary but are configured
entirely in the web UI - no `rclone config` on a terminal, no mounted
rclone.conf.

For every operation a throw-away rclone.conf (plus, for SFTP, a known_hosts
file) is generated from the target's stored settings into a private temp
directory, handed to rclone/restic via --config / RCLONE_CONFIG, and deleted
again afterwards. Passwords are passed through `rclone obscure`, as rclone
requires for `pass`-type options.

Supported target types (see TARGET_TYPES):
  - sftp:          any SSH/SFTP server (Hetzner Storage Box, rsync.net, a
                   Linux server, a second NAS)
  - webdav:        Nextcloud, ownCloud, IONOS HiDrive, MagentaCLOUD, ...
  - ftp:           FTP/FTPS (Fritz!Box NAS, older NAS models)
  - azure_blob:    Microsoft Azure Blob Storage
  - dropbox:       Dropbox, via an in-app sign-in (own Dropbox app)
  - rclone_cloud:  further cloud services with a small form each (pCloud,
                   Box, Mega, Koofr, Storj) plus an expert mode for any
                   other rclone backend
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import requests

from app.config import BASE_DIR

REMOTE_NAME = "dbm_remote"
TMP_DIR_PREFIX = "rclone_tmp_"

TARGET_TYPES = ("sftp", "webdav", "ftp", "azure_blob", "dropbox", "rclone_cloud")

# Fields that are never sent back to the browser (shown as "***") and are
# kept from the stored config when the browser sends "***" back on edit.
SECRET_FIELDS: dict[str, list[str]] = {
    "sftp": ["password", "private_key", "key_passphrase"],
    "webdav": ["password"],
    "ftp": ["password"],
    "azure_blob": ["key", "sas_url"],
    "dropbox": ["app_secret", "refresh_token"],
    "rclone_cloud": ["password", "token", "access_grant", "options"],
}

# Backends offered in the "rclone_cloud" form, with the fields each one needs.
CLOUD_PROVIDERS = ("pcloud", "box", "mega", "koofr", "storj", "custom")

WEBDAV_VENDORS = ("nextcloud", "owncloud", "sharepoint", "other")

_OPTION_KEY_RE = re.compile(r"^[a-z0-9_]+$")
_BACKEND_RE = re.compile(r"^[a-z0-9]+$")


class ConfigError(ValueError):
    """Raised for missing/invalid settings - the message is shown in the UI."""


def is_managed(target_type: str) -> bool:
    return target_type in TARGET_TYPES


# ---------------------------------------------------------------- helpers

def obscure(plain: str) -> str:
    """rclone stores `pass`-type options obscured; plain text is rejected."""
    proc = subprocess.run(["rclone", "obscure", "-"], input=plain, capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(f"rclone obscure fehlgeschlagen: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _clean(value, field: str) -> str:
    """A config value as a single line. A newline would let a value start a
    new line in the generated rclone.conf and inject further options."""
    text = "" if value is None else str(value).strip()
    if "\n" in text or "\r" in text:
        raise ConfigError(f"Feld '{field}' darf keinen Zeilenumbruch enthalten.")
    return text


def _require(config: dict, field: str, label: str) -> str:
    value = _clean(config.get(field), field)
    if not value:
        raise ConfigError(f"{label} fehlt.")
    return value


def _port(config: dict, default: int) -> str:
    raw = str(config.get("port") or default).strip()
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise ConfigError(f"Ungültiger Port: {raw}")
    return raw


def _single_line_json(raw: str, field: str) -> str:
    try:
        return json.dumps(json.loads(raw), separators=(",", ":"))
    except (TypeError, ValueError):
        raise ConfigError(f"{field} ist kein gültiges JSON (Ausgabe von 'rclone authorize' komplett einfügen).")


def _path(config: dict, field: str = "path") -> str:
    return _clean(config.get(field), field).replace("\\", "/").strip("/")


def _known_hosts_line(config: dict, host: str, port: str) -> str:
    """Accepts either a full known_hosts line ("host ssh-ed25519 AAAA...") or
    just the key part ("ssh-ed25519 AAAA..."), as printed by ssh-keyscan."""
    raw = _clean(config.get("host_key"), "host_key")
    if not raw:
        return ""
    parts = raw.split()
    if len(parts) >= 2 and parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        host_field = host if port == "22" else f"[{host}]:{port}"
        parts = [host_field] + parts
    if len(parts) < 3 or not parts[1].startswith(("ssh-", "ecdsa-", "sk-")):
        raise ConfigError("Host-Key hat ein unbekanntes Format (erwartet z. B. 'ssh-ed25519 AAAA...').")
    return " ".join(parts[:3])


# ---------------------------------------------------------------- per type

def _sftp(config: dict, files: dict) -> tuple[dict, str]:
    host = _require(config, "host", "Host")
    port = _port(config, 22)
    opts = {"type": "sftp", "host": host, "port": port, "user": _require(config, "username", "Benutzername")}
    password = config.get("password") or ""
    private_key = (config.get("private_key") or "").strip()
    if not password and not private_key:
        raise ConfigError("Passwort oder privater SSH-Schlüssel angeben.")
    if password:
        opts["pass"] = obscure(password)
    if private_key:
        if "PRIVATE KEY" not in private_key:
            raise ConfigError("Privater Schlüssel muss im PEM/OpenSSH-Format sein (-----BEGIN ... PRIVATE KEY-----).")
        # rclone expects the PEM on one line with literal "\n" separators.
        opts["key_pem"] = "\\n".join(line.strip() for line in private_key.replace("\r", "").split("\n"))
        if config.get("key_passphrase"):
            opts["key_file_pass"] = obscure(config["key_passphrase"])
    known_host = _known_hosts_line(config, host, port)
    if known_host:
        files["known_hosts"] = known_host + "\n"
        opts["known_hosts_file"] = "{dir}/known_hosts"
    # Don't keep idle SSH connections around forever between operations.
    opts["idle_timeout"] = "1m"
    return opts, _path(config)


def _webdav(config: dict, files: dict) -> tuple[dict, str]:
    url = _require(config, "url", "WebDAV-URL")
    if not url.startswith(("https://", "http://")):
        raise ConfigError("WebDAV-URL muss mit https:// oder http:// beginnen.")
    vendor = _clean(config.get("vendor"), "vendor") or "other"
    if vendor not in WEBDAV_VENDORS:
        raise ConfigError(f"Unbekannter WebDAV-Anbieter: {vendor}")
    opts = {"type": "webdav", "url": url, "vendor": vendor}
    user = _clean(config.get("username"), "username")
    if user:
        opts["user"] = user
    if config.get("password"):
        opts["pass"] = obscure(config["password"])
    return opts, _path(config)


def _ftp(config: dict, files: dict) -> tuple[dict, str]:
    opts = {
        "type": "ftp",
        "host": _require(config, "host", "Host"),
        "port": _port(config, 21),
        "user": _require(config, "username", "Benutzername"),
    }
    if config.get("password"):
        opts["pass"] = obscure(config["password"])
    tls = _clean(config.get("tls"), "tls") or "explicit"
    if tls == "explicit":
        opts["explicit_tls"] = "true"
    elif tls == "implicit":
        opts["tls"] = "true"
    elif tls != "none":
        raise ConfigError(f"Unbekannter TLS-Modus: {tls}")
    if tls != "none" and config.get("skip_cert_check"):
        opts["no_check_certificate"] = "true"
    return opts, _path(config)


def _azure_blob(config: dict, files: dict) -> tuple[dict, str]:
    container = _require(config, "container", "Container")
    opts = {"type": "azureblob"}
    sas_url = _clean(config.get("sas_url"), "sas_url")
    if sas_url:
        opts["sas_url"] = sas_url
    else:
        opts["account"] = _require(config, "account", "Speicherkonto")
        opts["key"] = _require(config, "key", "Zugriffsschlüssel")
    tier = _clean(config.get("access_tier"), "access_tier")
    if tier:
        # "archive" is deliberately not offered: archived blobs need hours of
        # rehydration before a restore could even start.
        if tier not in ("hot", "cool", "cold"):
            raise ConfigError(f"Ungültige Zugriffsebene: {tier}")
        opts["access_tier"] = tier.capitalize()
    base = "/".join(p for p in (container, _path(config, "prefix")) if p)
    return opts, base


def _dropbox(config: dict, files: dict) -> tuple[dict, str]:
    refresh = _clean(config.get("refresh_token"), "refresh_token")
    if not refresh:
        raise ConfigError("Dropbox ist nicht verbunden - bitte zuerst anmelden.")
    token = {
        "access_token": "",
        "token_type": "bearer",
        "refresh_token": refresh,
        # Already expired, so rclone fetches a fresh access token right away
        # using the refresh token + app key/secret.
        "expiry": "2000-01-01T00:00:00Z",
    }
    opts = {
        "type": "dropbox",
        "client_id": _require(config, "app_key", "App-Key"),
        "client_secret": _require(config, "app_secret", "App-Secret"),
        "token": json.dumps(token, separators=(",", ":")),
    }
    return opts, _path(config, "folder_path")


def _rclone_cloud(config: dict, files: dict) -> tuple[dict, str]:
    provider = _clean(config.get("provider"), "provider")
    if provider == "pcloud":
        opts = {"type": "pcloud", "token": _single_line_json(_require(config, "token", "Token"), "Token")}
        hostname = _clean(config.get("hostname"), "hostname") or "eapi.pcloud.com"
        if hostname not in ("api.pcloud.com", "eapi.pcloud.com"):
            raise ConfigError("pCloud-Region muss api.pcloud.com (USA) oder eapi.pcloud.com (EU) sein.")
        opts["hostname"] = hostname
    elif provider == "box":
        opts = {"type": "box", "token": _single_line_json(_require(config, "token", "Token"), "Token")}
    elif provider == "mega":
        opts = {"type": "mega", "user": _require(config, "username", "E-Mail"),
                "pass": obscure(_require(config, "password", "Passwort"))}
    elif provider == "koofr":
        opts = {"type": "koofr", "provider": "koofr", "user": _require(config, "username", "E-Mail"),
                "password": obscure(_require(config, "password", "App-Passwort"))}
    elif provider == "storj":
        opts = {"type": "storj", "provider": "existing",
                "access_grant": _require(config, "access_grant", "Access Grant")}
    elif provider == "custom":
        backend = _require(config, "backend", "rclone-Backend")
        if not _BACKEND_RE.match(backend):
            raise ConfigError("rclone-Backend darf nur Kleinbuchstaben und Ziffern enthalten (z. B. 'b2').")
        opts = {"type": backend}
        for n, line in enumerate((config.get("options") or "").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not sep or not _OPTION_KEY_RE.match(key) or key == "type":
                raise ConfigError(f"Zeile {n} der Optionen ist ungültig (Format: schluessel = wert).")
            opts[key] = obscure(value) if key in ("pass", "password") else value
    else:
        raise ConfigError(f"Unbekannter Cloud-Anbieter: {provider or '(leer)'}")
    return opts, _path(config)


_BUILDERS = {
    "sftp": _sftp,
    "webdav": _webdav,
    "ftp": _ftp,
    "azure_blob": _azure_blob,
    "dropbox": _dropbox,
    "rclone_cloud": _rclone_cloud,
}


def build_remote(target_type: str, config: dict) -> tuple[dict, dict, str]:
    """Returns (rclone options, extra files {name: content}, base path inside
    the remote). Raises ConfigError on missing/invalid settings."""
    builder = _BUILDERS.get(target_type)
    if not builder:
        raise ValueError(f"Kein rclone-basierter Zieltyp: {target_type}")
    files: dict = {}
    opts, base = builder(config, files)
    for key, value in opts.items():
        _clean(value, key)
    return opts, files, base


def write_conf(target_type: str, config: dict) -> tuple[str, dict]:
    """Writes a private temp directory holding rclone.conf (+ extra files).
    Returns (path to rclone.conf, {"remote", "remote_path"}) - the second
    value is a config dict the plain "rclone" target code understands.
    The caller must call discard_conf(path) afterwards."""
    opts, files, base = build_remote(target_type, config)
    tmp_root = BASE_DIR / ".tmp"
    tmp_root.mkdir(exist_ok=True)
    tmp_dir = tmp_root / f"{TMP_DIR_PREFIX}{secrets.token_hex(8)}"
    tmp_dir.mkdir(mode=0o700)
    try:
        for name, content in files.items():
            p = tmp_dir / name
            p.write_text(content)
            os.chmod(p, 0o600)
        lines = [f"[{REMOTE_NAME}]"]
        for key, value in opts.items():
            if value == "{dir}/known_hosts":
                value = str(tmp_dir / "known_hosts")
            lines.append(f"{key} = {value}")
        conf = tmp_dir / "rclone.conf"
        conf.write_text("\n".join(lines) + "\n")
        os.chmod(conf, 0o600)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return str(conf), {"remote": REMOTE_NAME, "remote_path": base}


def discard_conf(conf_path) -> None:
    """Deletes a temp rclone.conf written by write_conf() (its whole private
    directory) or by restic_engine._write_smb_rclone_conf (a single file)."""
    if not conf_path:
        return
    p = Path(conf_path)
    try:
        if p.parent.name.startswith(TMP_DIR_PREFIX):
            shutil.rmtree(p.parent, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def materialized(target_type: str, config: dict) -> Iterator[tuple[str, dict]]:
    conf_path, rclone_config = write_conf(target_type, config)
    try:
        yield conf_path, rclone_config
    finally:
        discard_conf(conf_path)


# ---------------------------------------------------------------- Dropbox sign-in

DROPBOX_AUTH_URL = "https://www.dropbox.com/oauth2/authorize"
DROPBOX_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
DROPBOX_ACCOUNT_URL = "https://api.dropboxapi.com/2/users/get_current_account"

_PENDING_TTL_SECONDS = 15 * 60
_dropbox_pending: dict[str, dict] = {}
_dropbox_lock = threading.Lock()


def dropbox_auth_url(app_key: str) -> str:
    """Dropbox's "no redirect" code flow: the user signs in on dropbox.com and
    gets a code to paste back into the app. Unlike a redirect flow this works
    for installs only reachable as http://<LAN-IP> (Dropbox only accepts
    https or localhost redirect URIs)."""
    app_key = _clean(app_key, "app_key")
    if not app_key:
        raise ConfigError("App-Key fehlt.")
    return (f"{DROPBOX_AUTH_URL}?client_id={requests.utils.quote(app_key)}"
            "&response_type=code&token_access_type=offline")


def dropbox_exchange_code(app_key: str, app_secret: str, code: str) -> dict:
    """Exchanges the pasted code for a refresh token and parks it server-side.
    Returns {"pending_id", "account"} - the refresh token never goes back to
    the browser; create/update resolves pending_id via dropbox_pop_pending."""
    app_key, app_secret, code = (_clean(v, n) for v, n in ((app_key, "app_key"), (app_secret, "app_secret"), (code, "code")))
    if not (app_key and app_secret and code):
        raise ConfigError("App-Key, App-Secret und Code werden benötigt.")
    resp = requests.post(DROPBOX_TOKEN_URL, data={
        "code": code, "grant_type": "authorization_code",
        "client_id": app_key, "client_secret": app_secret,
    }, timeout=30)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error_description") or resp.json().get("error")
        except ValueError:
            detail = resp.text[:200]
        raise ConfigError(f"Dropbox-Anmeldung fehlgeschlagen: {detail}")
    tokens = resp.json()
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise ConfigError("Dropbox hat keinen Refresh-Token geliefert - bitte erneut anmelden.")
    account = ""
    try:
        info = requests.post(DROPBOX_ACCOUNT_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"},
                             timeout=30)
        if info.status_code == 200:
            account = info.json().get("email", "")
    except requests.RequestException:
        pass
    pending_id = secrets.token_urlsafe(24)
    with _dropbox_lock:
        cutoff = time.time() - _PENDING_TTL_SECONDS
        for key in [k for k, v in _dropbox_pending.items() if v["created_at"] < cutoff]:
            _dropbox_pending.pop(key, None)
        _dropbox_pending[pending_id] = {"refresh_token": refresh, "account": account,
                                        "app_key": app_key, "created_at": time.time()}
    return {"pending_id": pending_id, "account": account}


def dropbox_pop_pending(pending_id: str, app_key: str) -> dict:
    with _dropbox_lock:
        data = _dropbox_pending.pop(pending_id or "", None)
    if not data or time.time() - data["created_at"] > _PENDING_TTL_SECONDS:
        raise ConfigError("Die Dropbox-Anmeldung ist abgelaufen - bitte erneut verbinden.")
    if data["app_key"] != app_key:
        raise ConfigError("App-Key wurde nach der Anmeldung geändert - bitte erneut verbinden.")
    return data
