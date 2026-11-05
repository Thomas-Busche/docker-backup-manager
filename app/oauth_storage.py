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

import datetime
import secrets
import time
from pathlib import Path
from typing import Optional

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

_PENDING_TTL_SECONDS = 15 * 60
_pending: dict[str, dict] = {}  # state -> {provider, refresh_token, account, created_at}


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
    return url, state


def handle_callback(provider: str, code: str, state: str) -> None:
    """Exchanges the authorization code for tokens and stashes the refresh
    token server-side under `state`, for oauth-complete to pick up."""
    client_id, client_secret = _get_credentials(provider)
    if provider == "google":
        resp = requests.post(GOOGLE_TOKEN_URL, data={
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
        resp = requests.post(MS_TOKEN_URL, data={
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
    resp = requests.get("https://www.googleapis.com/oauth2/v2/userinfo",
                         headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("email", "")


def _onedrive_account_email(access_token: str) -> str:
    try:
        resp = requests.get("https://graph.microsoft.com/v1.0/me",
                             headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("mail") or data.get("userPrincipalName", "")
    except Exception:
        return ""


def _refresh_access_token(provider: str, refresh_token: str) -> str:
    client_id, client_secret = _get_credentials(provider)
    if provider == "google":
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        }, timeout=30)
    elif provider == "onedrive":
        resp = requests.post(MS_TOKEN_URL, data={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "scope": MS_SCOPE,
        }, timeout=30)
    else:
        raise ValueError(f"Unknown OAuth provider: {provider}")
    resp.raise_for_status()
    return resp.json()["access_token"]


def _relative_key(backup_path: Path) -> str:
    from app.storage_sync import _relative_key as shared_relative_key
    return shared_relative_key(backup_path)


# ---------- Google Drive ----------

def _gdrive_ensure_folder(access_token: str, parent_id: str, name: str) -> str:
    headers = {"Authorization": f"Bearer {access_token}"}
    query = (
        f"name = '{name}' and '{parent_id}' in parents and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    resp = requests.get("https://www.googleapis.com/drive/v3/files",
                         headers=headers, params={"q": query, "fields": "files(id)"}, timeout=15)
    resp.raise_for_status()
    files = resp.json().get("files", [])
    if files:
        return files[0]["id"]

    resp = requests.post("https://www.googleapis.com/drive/v3/files", headers=headers, json={
        "name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id],
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["id"]


def _gdrive_resolve_folder(access_token: str, folder_path: str) -> str:
    folder_id = "root"
    for part in [p for p in folder_path.split("/") if p]:
        folder_id = _gdrive_ensure_folder(access_token, folder_id, part)
    return folder_id


def _gdrive_find_folder(access_token: str, parent_id: str, name: str) -> Optional[str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    query = (
        f"name = '{name}' and '{parent_id}' in parents and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    resp = requests.get("https://www.googleapis.com/drive/v3/files",
                         headers=headers, params={"q": query, "fields": "files(id)"}, timeout=15)
    resp.raise_for_status()
    files = resp.json().get("files", [])
    return files[0]["id"] if files else None


def delete_google_drive(config: dict, relative_key: str) -> None:
    access_token = _refresh_access_token("google", config["refresh_token"])
    folder_id = _gdrive_find_folder_chain(access_token, config.get("folder_path", ""), relative_key)
    if not folder_id:
        return  # nothing uploaded (or already deleted) - nothing to do
    resp = requests.delete(f"https://www.googleapis.com/drive/v3/files/{folder_id}",
                            headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if resp.status_code not in (204, 404):
        resp.raise_for_status()


def _gdrive_find_folder_chain(access_token: str, folder_path: str, relative_key: str) -> Optional[str]:
    folder_id = "root"
    for part in [p for p in folder_path.split("/") if p] + [p for p in relative_key.split("/") if p]:
        folder_id = _gdrive_find_folder(access_token, folder_id, part)
        if not folder_id:
            return None
    return folder_id


def check_google_drive_connection(config: dict) -> None:
    access_token = _refresh_access_token("google", config["refresh_token"])
    _gdrive_resolve_folder(access_token, config.get("folder_path", ""))


def sync_google_drive(backup_path: Path, config: dict) -> None:
    access_token = _refresh_access_token("google", config["refresh_token"])
    key_root = _relative_key(backup_path)
    base_folder_id = _gdrive_resolve_folder(access_token, config.get("folder_path", ""))

    for file_path in Path(backup_path).rglob("*"):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(backup_path)
        parent_id = base_folder_id
        for part in [key_root, *rel.parts[:-1]]:
            if part and part != ".":
                parent_id = _gdrive_ensure_folder(access_token, parent_id, part)

        with open(file_path, "rb") as fh:
            init = requests.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json={"name": file_path.name, "parents": [parent_id]},
                timeout=30,
            )
            init.raise_for_status()
            upload_url = init.headers["Location"]
            put = requests.put(upload_url, data=fh, timeout=3600)
            put.raise_for_status()


# ---------- OneDrive ----------

def check_onedrive_connection(config: dict) -> None:
    access_token = _refresh_access_token("onedrive", config["refresh_token"])
    resp = requests.get("https://graph.microsoft.com/v1.0/me/drive",
                         headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    resp.raise_for_status()


def delete_onedrive(config: dict, relative_key: str) -> None:
    access_token = _refresh_access_token("onedrive", config["refresh_token"])
    folder_path = config.get("folder_path", "").strip("/")
    remote_parts = [p for p in [folder_path, relative_key] if p and p != "."]
    remote_path = "/".join(remote_parts)
    resp = requests.delete(f"https://graph.microsoft.com/v1.0/me/drive/root:/{remote_path}",
                            headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if resp.status_code not in (204, 404):
        resp.raise_for_status()


def sync_onedrive(backup_path: Path, config: dict) -> None:
    access_token = _refresh_access_token("onedrive", config["refresh_token"])
    key_root = _relative_key(backup_path)
    folder_path = config.get("folder_path", "").strip("/")

    for file_path in Path(backup_path).rglob("*"):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(backup_path)
        remote_parts = [p for p in [folder_path, key_root, str(rel).replace("\\", "/")] if p and p != "."]
        remote_path = "/".join(remote_parts)

        size = file_path.stat().st_size
        headers = {"Authorization": f"Bearer {access_token}"}
        if size <= 4 * 1024 * 1024:
            with open(file_path, "rb") as fh:
                resp = requests.put(
                    f"https://graph.microsoft.com/v1.0/me/drive/root:/{remote_path}:/content",
                    headers=headers, data=fh, timeout=120,
                )
            resp.raise_for_status()
            continue

        session = requests.post(
            f"https://graph.microsoft.com/v1.0/me/drive/root:/{remote_path}:/createUploadSession",
            headers=headers, json={"item": {"@microsoft.graph.conflictBehavior": "replace"}}, timeout=30,
        )
        session.raise_for_status()
        upload_url = session.json()["uploadUrl"]

        chunk_size = 10 * 1024 * 1024  # must be a multiple of 320 KiB per the Graph API
        with open(file_path, "rb") as fh:
            offset = 0
            while offset < size:
                chunk = fh.read(chunk_size)
                end = offset + len(chunk) - 1
                resp = requests.put(
                    upload_url,
                    headers={"Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{size}"},
                    data=chunk, timeout=300,
                )
                resp.raise_for_status()
                offset += len(chunk)
