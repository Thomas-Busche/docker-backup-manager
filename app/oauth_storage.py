"""
OAuth-based storage targets: Google Drive and OneDrive.

Unlike the `rclone` target (which needs `rclone config` run on a terminal and
an rclone.conf file mounted into the container), these targets are set up
entirely from the web UI: click "Mit Google/Microsoft anmelden", sign in in
a popup, done. This needs a one-time app registration by the operator
(Google Cloud Console / Azure AD - see README), since Google/Microsoft only
allow OAuth redirects to URIs an app owner has explicitly registered.

Flow:
  1. Frontend opens a popup to /api/settings/oauth/<provider>/start, which
     redirects to the provider's consent screen.
  2. After consent, the provider redirects to /api/settings/oauth/<provider>/callback
     with an authorization code. That's exchanged here for a refresh token,
     which is stashed server-side (in memory, keyed by a short-lived random
     state token) - never sent to the browser.
  3. The callback page postMessages the state back to the opener window and
     closes itself. The frontend then calls
     /api/settings/storage-targets/oauth-complete with that state + a chosen
     name/folder to actually create the StorageTarget row.

Access tokens expire in ~1h, so every upload refreshes one from the stored
refresh_token first rather than caching it.
"""
from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

from app.config import (
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT, PUBLIC_URL,
)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"

MS_AUTH_URL = f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/authorize"
MS_TOKEN_URL = f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/token"
MS_SCOPE = "offline_access Files.ReadWrite User.Read"

# Device Code Flow for OneDrive (no Azure app registration needed by the user).
# Microsoft Graph tutorial / SDK sample client — a genuine public client
# (allowPublicClient=true, no client_secret required) designed for device flow + Graph API.
# Users see a standard Microsoft consent screen for the requested scopes.
_MS_DEVICE_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
_MS_DEVICE_CODE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
_MS_DEVICE_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

_PENDING_TTL_SECONDS = 15 * 60
_pending: dict[str, dict] = {}  # state -> {provider, refresh_token, account, created_at}
_issued_states: dict[str, dict] = {}  # state -> {provider, created_at}; only these are accepted by the callback

logger = logging.getLogger("dbm.oauth_storage")

# Access tokens from both Google and Microsoft live ~60 min. Long syncs (many
# files / large files) must get a fresh one before it runs out mid-upload.
_ACCESS_TOKEN_MAX_AGE = 45 * 60

# Transient HTTP statuses worth retrying (rate limiting + server-side hiccups),
# as recommended by both the Google Drive and Microsoft Graph docs.
_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_MAX_RETRIES = 5

# Google resumable uploads: chunk size must be a multiple of 256 KiB.
# Microsoft Graph upload sessions: chunk size must be a multiple of 320 KiB.
# 10 MiB = 40 * 256 KiB = 32 * 320 KiB, so one size satisfies both APIs.
_UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024


class OAuthTokenRevoked(RuntimeError):
    """The stored refresh token is no longer accepted - the user must reconnect."""


def _retry_delay(resp: Optional[requests.Response], attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return min(float(retry_after), 120.0)
    return min(2.0 ** attempt, 60.0)


def _request(method: str, url: str, *, retries: int = _MAX_RETRIES, **kwargs) -> requests.Response:
    """requests.request() with a mandatory timeout and retries on network
    errors and transient 408/429/5xx answers (honouring Retry-After).
    Bodies that are open files must not be passed here - they cannot be
    replayed; uploads handle their own retries chunk by chunk."""
    kwargs.setdefault("timeout", 60)
    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if attempt >= retries:
                raise
            time.sleep(_retry_delay(None, attempt))
            continue
        if resp.status_code in _RETRY_STATUSES and attempt < retries:
            time.sleep(_retry_delay(resp, attempt))
            continue
        return resp
    raise AssertionError("unreachable")
_device_sessions: dict[str, dict] = {}  # key -> {device_code, interval, expires_at, state_or_none}


def _get_credentials(provider: str) -> tuple[str, str]:
    """Return (client_id, client_secret) for a provider.
    Env vars take priority; falls back to values stored in AppSetting via the UI wizard."""
    if provider == "google":
        if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
            return GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
        key_id, key_sec = "oauth_google_client_id", "oauth_google_client_secret"
    elif provider == "onedrive":
        if MS_CLIENT_ID and MS_CLIENT_SECRET:
            return MS_CLIENT_ID, MS_CLIENT_SECRET
        key_id, key_sec = "oauth_ms_client_id", "oauth_ms_client_secret"
    else:
        return "", ""

    from app.database import SessionLocal
    from app.models import AppSetting
    with SessionLocal() as db:
        row_id = db.query(AppSetting).filter_by(key=key_id).first()
        row_sec = db.query(AppSetting).filter_by(key=key_sec).first()
        return (row_id.value if row_id else ""), (row_sec.value if row_sec else "")


class OAuthNotConfigured(Exception):
    pass


def _get_public_url() -> str:
    """Return PUBLIC_URL from env var or DB fallback."""
    if PUBLIC_URL:
        return PUBLIC_URL
    from app.database import SessionLocal
    from app.models import AppSetting
    with SessionLocal() as db:
        row = db.query(AppSetting).filter_by(key="public_url").first()
        return row.value.rstrip("/") if row and row.value else ""


def _require_public_url() -> str:
    url = _get_public_url()
    if not url:
        raise OAuthNotConfigured(
            "DBM_PUBLIC_URL ist nicht gesetzt - wird als OAuth-Redirect-Adresse benötigt "
            "(z.B. http://192.168.1.10:8420). Im Einrichtungs-Assistenten oben eintragen oder "
            "in der docker-compose.yml setzen."
        )
    return url


def redirect_uri(provider: str) -> str:
    return f"{_require_public_url()}/api/settings/oauth/{provider}/callback"


def _cleanup_pending() -> None:
    cutoff = time.time() - _PENDING_TTL_SECONDS
    for state in [s for s, v in _pending.items() if v["created_at"] < cutoff]:
        _pending.pop(state, None)
    for state in [s for s, v in _issued_states.items() if v["created_at"] < cutoff]:
        _issued_states.pop(state, None)


def build_auth_url(provider: str) -> tuple[str, str]:
    """Returns (auth_url, state). The state is also the key to later retrieve
    the exchanged tokens once the provider redirects back."""
    _cleanup_pending()
    state = secrets.token_urlsafe(24)
    client_id, client_secret = _get_credentials(provider)
    if provider == "google":
        if not client_id or not client_secret:
            raise OAuthNotConfigured(
                "Google Drive ist nicht konfiguriert. Bitte Client-ID und Secret über den "
                "Einrichtungs-Assistenten eingeben oder DBM_GOOGLE_CLIENT_ID/DBM_GOOGLE_CLIENT_SECRET "
                "in der docker-compose.yml setzen."
            )
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri("google"),
            "response_type": "code",
            "scope": GOOGLE_DRIVE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        url = GOOGLE_AUTH_URL + "?" + "&".join(f"{k}={requests.utils.quote(v)}" for k, v in params.items())
    elif provider == "onedrive":
        if not client_id or not client_secret:
            raise OAuthNotConfigured(
                "OneDrive ist nicht konfiguriert. Bitte Client-ID und Secret über den "
                "Einrichtungs-Assistenten eingeben oder DBM_MS_CLIENT_ID/DBM_MS_CLIENT_SECRET "
                "in der docker-compose.yml setzen."
            )
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri("onedrive"),
            "response_type": "code",
            "scope": MS_SCOPE,
            "state": state,
        }
        url = MS_AUTH_URL + "?" + "&".join(f"{k}={requests.utils.quote(v)}" for k, v in params.items())
    else:
        raise ValueError(f"Unknown OAuth provider: {provider}")
    _issued_states[state] = {"provider": provider, "created_at": time.time()}
    return url, state


def handle_callback(provider: str, code: str, state: str) -> None:
    """Exchanges the authorization code for tokens and stashes the refresh
    token server-side under `state`, for oauth-complete to pick up."""
    # The state must be one this server handed out for this provider (CSRF
    # protection per RFC 6749 section 10.12) and is single-use.
    _cleanup_pending()
    issued = _issued_states.pop(state, None) if state else None
    if not issued or issued["provider"] != provider:
        raise ValueError("Ungültige oder abgelaufene Anmeldung - bitte erneut verbinden.")
    if not code:
        raise ValueError("Keine Autorisierung vom Anbieter erhalten - bitte erneut verbinden.")
    client_id, client_secret = _get_credentials(provider)
    if provider == "google":
        resp = _request("POST", GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri("google"),
            "grant_type": "authorization_code",
        }, timeout=30)
        resp.raise_for_status()
        tokens = resp.json()
        account = _google_account_email(tokens["access_token"])
    elif provider == "onedrive":
        resp = _request("POST", MS_TOKEN_URL, data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri("onedrive"),
            "grant_type": "authorization_code",
            "scope": MS_SCOPE,
        }, timeout=30)
        resp.raise_for_status()
        tokens = resp.json()
        account = _onedrive_account_email(tokens["access_token"])
    else:
        raise ValueError(f"Unknown OAuth provider: {provider}")

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Anbieter hat keinen refresh_token zurückgegeben (Zugriff erneut widerrufen und neu verbinden).")

    _pending[state] = {
        "provider": provider, "refresh_token": refresh_token, "account": account, "created_at": time.time(),
    }


def pop_pending(state: str) -> dict:
    _cleanup_pending()
    data = _pending.pop(state, None)
    if not data:
        raise ValueError("Diese Anmeldung ist abgelaufen oder unbekannt - bitte erneut verbinden.")
    return data


def _google_account_email(access_token: str) -> str:
    # drive.file alone doesn't grant the userinfo scope - the account name is
    # only cosmetic, so never fail the whole sign-in because of it.
    try:
        resp = _request("GET", "https://www.googleapis.com/drive/v3/about",
                        params={"fields": "user(emailAddress)"},
                        headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("user", {}).get("emailAddress", "")
    except Exception:  # noqa: BLE001
        return ""


def _onedrive_account_email(access_token: str) -> str:
    try:
        resp = _request("GET", "https://graph.microsoft.com/v1.0/me",
                             headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("mail") or data.get("userPrincipalName", "")
    except Exception:
        return ""


def _refresh_access_token(provider: str, refresh_token: str, config: dict | None = None) -> str:
    """Exchanges the stored refresh token for a fresh access token.

    Microsoft rotates refresh tokens: every refresh returns a new one, and the
    old one expires on its own (90 days by default). Google may also return a
    new one. A rotated token is written back into `config` and persisted to the
    StorageTarget row, otherwise OneDrive targets silently stop working ~90
    days after sign-in."""
    # Device-flow tokens must be refreshed with the same public client (no secret)
    # against the same authority ("common") they were issued by.
    device_client_id = (config or {}).get("device_client_id", "")
    if device_client_id:
        client_id, client_secret = device_client_id, ""
    else:
        client_id, client_secret = _get_credentials(provider)

    if provider == "google":
        data = {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "grant_type": "refresh_token",
        }
        if client_secret:
            data["client_secret"] = client_secret
        resp = _request("POST", GOOGLE_TOKEN_URL, data=data, timeout=30)
    elif provider == "onedrive":
        data = {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "grant_type": "refresh_token",
            "scope": MS_SCOPE,
        }
        if client_secret:
            data["client_secret"] = client_secret
        token_url = _MS_DEVICE_TOKEN_URL if device_client_id else MS_TOKEN_URL
        resp = _request("POST", token_url, data=data, timeout=30)
    else:
        raise ValueError(f"Unknown OAuth provider: {provider}")

    if resp.status_code in (400, 401):
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if body.get("error") in ("invalid_grant", "unauthorized_client", "invalid_client", "interaction_required"):
            name = "Google Drive" if provider == "google" else "OneDrive"
            raise OAuthTokenRevoked(
                f"{name}-Anmeldung ist abgelaufen oder wurde widerrufen "
                f"({body.get('error_description') or body.get('error')}). "
                "Bitte das Speicherziel unter Einstellungen erneut verbinden."
            )
    resp.raise_for_status()
    tokens = resp.json()

    new_refresh = tokens.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        if config is not None:
            config["refresh_token"] = new_refresh
        _persist_rotated_refresh_token(provider, refresh_token, new_refresh)
    return tokens["access_token"]


def _persist_rotated_refresh_token(provider: str, old_token: str, new_token: str) -> None:
    """Best-effort: store a rotated refresh token on every StorageTarget that
    still holds the old one. Never raises - a failed write only means the next
    run uses the old token, which stays valid until its own expiry."""
    import json

    target_type = "google_drive" if provider == "google" else "onedrive"
    try:
        from app.database import SessionLocal
        from app.models import StorageTarget
        with SessionLocal() as db:
            changed = False
            for target in db.query(StorageTarget).filter(StorageTarget.type == target_type).all():
                cfg = json.loads(target.config_json or "{}")
                if cfg.get("refresh_token") == old_token:
                    cfg["refresh_token"] = new_token
                    target.config_json = json.dumps(cfg)
                    changed = True
            if changed:
                db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist rotated %s refresh token", provider)


class _AccessToken:
    """Hands out an access token and transparently refreshes it before it
    expires, so a sync that runs longer than an hour doesn't start failing
    with 401 halfway through."""

    def __init__(self, provider: str, config: dict):
        self._provider = provider
        self._config = config
        self._token = ""
        self._obtained_at = 0.0

    def get(self, force: bool = False) -> str:
        if force or not self._token or time.monotonic() - self._obtained_at > _ACCESS_TOKEN_MAX_AGE:
            self._token = _refresh_access_token(self._provider, self._config["refresh_token"], self._config)
            self._obtained_at = time.monotonic()
        return self._token

    def headers(self, force: bool = False) -> dict:
        return {"Authorization": f"Bearer {self.get(force)}"}


def _authed(token: _AccessToken, method: str, url: str, **kwargs) -> requests.Response:
    """Authenticated API call; on a 401 the access token is refreshed once and
    the request repeated."""
    extra = kwargs.pop("headers", {}) or {}
    resp = _request(method, url, headers={**extra, **token.headers()}, **kwargs)
    if resp.status_code == 401:
        resp = _request(method, url, headers={**extra, **token.headers(force=True)}, **kwargs)
    return resp


def _relative_key(backup_path: Path) -> str:
    from app.storage_sync import _relative_key as shared_relative_key
    return shared_relative_key(backup_path)


# ---------- Google Drive ----------

_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
_FOLDER_MIME = "application/vnd.google-apps.folder"


def _drive_q_literal(value: str) -> str:
    """Escapes a value for a Drive search query string literal
    (backslash and single quote must be backslash-escaped)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _gdrive_find_child(token: _AccessToken, parent_id: str, name: str, folder: bool) -> Optional[str]:
    mime = f"mimeType = '{_FOLDER_MIME}'" if folder else f"mimeType != '{_FOLDER_MIME}'"
    query = f"name = {_drive_q_literal(name)} and {_drive_q_literal(parent_id)} in parents and {mime} and trashed = false"
    resp = _authed(token, "GET", _DRIVE_FILES_URL,
                   params={"q": query, "fields": "files(id)", "pageSize": 10, "spaces": "drive"}, timeout=30)
    resp.raise_for_status()
    files = resp.json().get("files", [])
    return files[0]["id"] if files else None


def _gdrive_ensure_folder(token: _AccessToken, parent_id: str, name: str) -> str:
    existing = _gdrive_find_child(token, parent_id, name, folder=True)
    if existing:
        return existing
    resp = _authed(token, "POST", _DRIVE_FILES_URL, params={"fields": "id"}, json={
        "name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id],
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def _gdrive_resolve_folder(token: _AccessToken, folder_path: str) -> str:
    folder_id = "root"
    for part in [p for p in folder_path.split("/") if p]:
        folder_id = _gdrive_ensure_folder(token, folder_id, part)
    return folder_id


def _gdrive_find_folder(token: _AccessToken, parent_id: str, name: str) -> Optional[str]:
    return _gdrive_find_child(token, parent_id, name, folder=True)


def delete_google_drive(config: dict, relative_key: str) -> None:
    token = _AccessToken("google", config)
    folder_id = _gdrive_find_folder_chain(token, config.get("folder_path", ""), relative_key)
    if not folder_id:
        return  # nothing uploaded (or already deleted) - nothing to do
    resp = _authed(token, "DELETE", f"{_DRIVE_FILES_URL}/{folder_id}", timeout=60)
    if resp.status_code not in (200, 204, 404):
        resp.raise_for_status()


def _gdrive_find_folder_chain(token: _AccessToken, folder_path: str, relative_key: str) -> Optional[str]:
    folder_id = "root"
    for part in [p for p in folder_path.split("/") if p] + [p for p in relative_key.split("/") if p]:
        folder_id = _gdrive_find_folder(token, folder_id, part)
        if not folder_id:
            return None
    return folder_id


def check_google_drive_connection(config: dict) -> None:
    token = _AccessToken("google", config)
    _gdrive_resolve_folder(token, config.get("folder_path", ""))


def _parse_range_end(range_header: str) -> int:
    """'bytes=0-1048575' -> 1048576 (number of bytes the server has)."""
    try:
        return int(range_header.rsplit("-", 1)[1]) + 1
    except (IndexError, ValueError):
        return 0


def _gdrive_upload_file(token: _AccessToken, file_path: Path, parent_id: str) -> None:
    """Resumable, chunked upload per the Drive API docs. Each chunk is retried
    on its own; after a failure the session is asked how many bytes it already
    has and the upload resumes from there. An existing file with the same name
    in the same folder (e.g. a retried sync) is updated instead of duplicated."""
    size = file_path.stat().st_size
    existing_id = _gdrive_find_child(token, parent_id, file_path.name, folder=False)
    if existing_id:
        init = _authed(token, "PATCH", f"{_DRIVE_UPLOAD_URL}/{existing_id}",
                       params={"uploadType": "resumable"},
                       headers={"X-Upload-Content-Length": str(size)}, json={}, timeout=60)
    else:
        init = _authed(token, "POST", _DRIVE_UPLOAD_URL, params={"uploadType": "resumable"},
                       headers={"X-Upload-Content-Length": str(size)},
                       json={"name": file_path.name, "parents": [parent_id]}, timeout=60)
    init.raise_for_status()
    upload_url = init.headers["Location"]

    if size == 0:
        resp = _request("PUT", upload_url, data=b"", headers={"Content-Length": "0"}, timeout=60)
        resp.raise_for_status()
        return

    offset = 0
    failures = 0
    with open(file_path, "rb") as fh:
        while offset < size:
            fh.seek(offset)
            chunk = fh.read(_UPLOAD_CHUNK_SIZE)
            end = offset + len(chunk) - 1
            try:
                resp = requests.put(upload_url, data=chunk, timeout=300, headers={
                    "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{size}",
                })
            except (requests.ConnectionError, requests.Timeout):
                resp = None
            if resp is not None and resp.status_code in (200, 201):
                return
            if resp is not None and resp.status_code == 308:
                offset = _parse_range_end(resp.headers.get("Range", "")) if resp.headers.get("Range") else 0
                failures = 0
                continue
            if resp is not None and resp.status_code not in _RETRY_STATUSES:
                resp.raise_for_status()
                raise RuntimeError(f"Google Drive Upload: unerwartete Antwort {resp.status_code}")
            failures += 1
            if failures > _MAX_RETRIES:
                if resp is not None:
                    resp.raise_for_status()
                raise RuntimeError(f"Google Drive Upload von {file_path.name} nach mehreren Versuchen abgebrochen")
            time.sleep(_retry_delay(resp, failures))
            # Ask the session where to continue from.
            status = _request("PUT", upload_url, data=b"", timeout=60,
                              headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"})
            if status.status_code in (200, 201):
                return
            if status.status_code == 308:
                offset = _parse_range_end(status.headers.get("Range", "")) if status.headers.get("Range") else 0
            elif status.status_code == 404:
                raise RuntimeError("Google Drive Upload-Sitzung abgelaufen - Sync bitte erneut starten")


def sync_google_drive(backup_path: Path, config: dict) -> None:
    token = _AccessToken("google", config)
    key_root = _relative_key(backup_path)
    base_folder_id = _gdrive_resolve_folder(token, config.get("folder_path", ""))
    folder_cache: dict[tuple, str] = {}

    for file_path in sorted(Path(backup_path).rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(backup_path)
        parent_id = base_folder_id
        parts: list[str] = []
        for part in [*key_root.split("/"), *rel.parts[:-1]]:
            if part and part != ".":
                parts.append(part)
                cache_key = tuple(parts)
                if cache_key not in folder_cache:
                    folder_cache[cache_key] = _gdrive_ensure_folder(token, parent_id, part)
                parent_id = folder_cache[cache_key]
        _gdrive_upload_file(token, file_path, parent_id)


# ---------- OneDrive ----------

_GRAPH_DRIVE = "https://graph.microsoft.com/v1.0/me/drive"


def _onedrive_item_path(*parts: str) -> str:
    """Builds the URL-encoded path for a /root:/<path>: address. Each segment
    is percent-encoded so names with spaces, '#', '%' or umlauts work."""
    segments: list[str] = []
    for part in parts:
        for seg in (part or "").replace("\\", "/").split("/"):
            if seg and seg != ".":
                segments.append(quote(seg, safe=""))
    return "/".join(segments)


def check_onedrive_connection(config: dict) -> None:
    token = _AccessToken("onedrive", config)
    resp = _authed(token, "GET", _GRAPH_DRIVE, timeout=30)
    resp.raise_for_status()


def delete_onedrive(config: dict, relative_key: str) -> None:
    token = _AccessToken("onedrive", config)
    remote_path = _onedrive_item_path(config.get("folder_path", ""), relative_key)
    if not remote_path:
        raise ValueError("OneDrive: leerer Pfad - Löschen des Stammordners verweigert")
    resp = _authed(token, "DELETE", f"{_GRAPH_DRIVE}/root:/{remote_path}", timeout=60)
    if resp.status_code not in (204, 404):
        resp.raise_for_status()


def _onedrive_upload_large(token: _AccessToken, file_path: Path, remote_path: str, size: int) -> None:
    """Upload session per the Graph docs: fragments are a multiple of 320 KiB,
    the uploadUrl is pre-authenticated (no Authorization header!). After a
    failed fragment the session's nextExpectedRanges tells where to resume."""
    session = _authed(token, "POST", f"{_GRAPH_DRIVE}/root:/{remote_path}:/createUploadSession",
                      json={"item": {"@microsoft.graph.conflictBehavior": "replace"}}, timeout=60)
    session.raise_for_status()
    upload_url = session.json()["uploadUrl"]

    offset = 0
    failures = 0
    try:
        with open(file_path, "rb") as fh:
            while offset < size:
                fh.seek(offset)
                chunk = fh.read(_UPLOAD_CHUNK_SIZE)
                end = offset + len(chunk) - 1
                try:
                    resp = requests.put(upload_url, data=chunk, timeout=300, headers={
                        "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{size}",
                    })
                except (requests.ConnectionError, requests.Timeout):
                    resp = None
                if resp is not None and resp.status_code in (200, 201):
                    return
                if resp is not None and resp.status_code == 202:
                    offset += len(chunk)
                    failures = 0
                    continue
                if resp is not None and resp.status_code not in _RETRY_STATUSES and resp.status_code != 416:
                    resp.raise_for_status()
                    raise RuntimeError(f"OneDrive Upload: unerwartete Antwort {resp.status_code}")
                failures += 1
                if failures > _MAX_RETRIES:
                    if resp is not None:
                        resp.raise_for_status()
                    raise RuntimeError(f"OneDrive Upload von {file_path.name} nach mehreren Versuchen abgebrochen")
                time.sleep(_retry_delay(resp, failures))
                status = _request("GET", upload_url, timeout=60)
                if status.status_code == 404:
                    raise RuntimeError("OneDrive Upload-Sitzung abgelaufen - Sync bitte erneut starten")
                status.raise_for_status()
                ranges = status.json().get("nextExpectedRanges") or []
                if not ranges:
                    return  # server has everything
                offset = int(str(ranges[0]).split("-")[0])
        raise RuntimeError("OneDrive Upload: Sitzung nicht abgeschlossen")
    except BaseException:
        # Free the server-side session so partial data doesn't linger.
        try:
            requests.delete(upload_url, timeout=30)
        except Exception:  # noqa: BLE001
            pass
        raise


def sync_onedrive(backup_path: Path, config: dict) -> None:
    token = _AccessToken("onedrive", config)
    key_root = _relative_key(backup_path)
    folder_path = config.get("folder_path", "")

    for file_path in sorted(Path(backup_path).rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(backup_path)
        remote_path = _onedrive_item_path(folder_path, key_root, rel.as_posix())
        size = file_path.stat().st_size

        # Simple upload is documented for files up to 250 MB but is a single,
        # non-resumable request - keep it for small files only.
        if size <= 4 * 1024 * 1024:
            data = file_path.read_bytes()
            resp = _authed(token, "PUT", f"{_GRAPH_DRIVE}/root:/{remote_path}:/content",
                           data=data, timeout=120)
            resp.raise_for_status()
            continue
        _onedrive_upload_large(token, file_path, remote_path, size)


# ── OneDrive Device Code Flow (no Azure app registration needed) ────────────

def device_flow_start() -> dict:
    """Request a device code from Microsoft and return display info.

    Returns a dict with: key, user_code, verification_uri, expires_in, interval.
    The 'key' is an opaque token the caller uses to poll device_flow_poll().
    """
    resp = _request("POST", _MS_DEVICE_CODE_URL, data={
        "client_id": _MS_DEVICE_CLIENT_ID,
        "scope": MS_SCOPE,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    key = secrets.token_urlsafe(24)
    _device_sessions[key] = {
        "device_code": data["device_code"],
        "interval": int(data.get("interval", 5)),
        "expires_at": time.time() + int(data.get("expires_in", 900)),
        "state": None,  # filled once token is received
        "last_poll": 0.0,
    }
    return {
        "key": key,
        "user_code": data["user_code"],
        "verification_uri": data.get("verification_uri", "https://microsoft.com/devicelogin"),
        "expires_in": int(data.get("expires_in", 900)),
        "interval": int(data.get("interval", 5)),
    }


def device_flow_poll(key: str) -> dict:
    """Poll Microsoft for the token.  Returns one of:
      {status: "pending"}              – user has not signed in yet
      {status: "ready", state: "..."}  – signed in; use state with pop_pending()
      {status: "expired"}              – code expired or session unknown
      {status: "error", detail: "..."}
    """
    session = _device_sessions.get(key)
    if not session:
        return {"status": "expired"}
    if time.time() > session["expires_at"]:
        _device_sessions.pop(key, None)
        return {"status": "expired"}

    # Already have a token from a previous poll
    if session["state"]:
        return {"status": "ready", "state": session["state"]}

    # Respect minimum poll interval
    if time.time() - session["last_poll"] < session["interval"]:
        return {"status": "pending"}
    session["last_poll"] = time.time()

    try:
        resp = requests.post(_MS_DEVICE_TOKEN_URL, data={
            "client_id": _MS_DEVICE_CLIENT_ID,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": session["device_code"],
        }, timeout=30)
        data = resp.json()
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}

    err = data.get("error", "")
    if err == "authorization_pending" or err == "slow_down":
        if err == "slow_down":
            session["interval"] = min(session["interval"] + 5, 30)
        return {"status": "pending"}
    if err:
        _device_sessions.pop(key, None)
        return {"status": "error", "detail": data.get("error_description", err)}

    refresh_token = data.get("refresh_token")
    if not refresh_token:
        return {"status": "error", "detail": "Kein refresh_token erhalten"}

    account = _onedrive_account_email(data["access_token"])
    state = secrets.token_urlsafe(24)
    _pending[state] = {
        "provider": "onedrive",
        "refresh_token": refresh_token,
        "account": account,
        "created_at": time.time(),
        "device_client_id": _MS_DEVICE_CLIENT_ID,
    }
    session["state"] = state
    return {"status": "ready", "state": state}
