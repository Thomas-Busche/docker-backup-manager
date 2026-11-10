"""Google Drive / OneDrive connector tests against a simulated HTTP API.

No real accounts are needed: every request the connector makes is answered by
FakeApi below, which mimics the documented behaviour of the Drive v3 and
Microsoft Graph upload/token endpoints closely enough to exercise retries,
resumes, token refresh and token rotation.
"""
import json
import time
from urllib.parse import unquote

import pytest
import requests


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None):
        self.status_code = status_code
        self._json = json_body
        self.headers = headers or {}
        self.text = json.dumps(json_body) if json_body is not None else ""

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


class FakeApi:
    """Records every call and dispatches it to a handler(method, url, kwargs)."""

    def __init__(self, monkeypatch, handler):
        from app import oauth_storage

        self.calls = []
        self.handler = handler

        def fake_request(method, url, **kwargs):
            self.calls.append((method.upper(), url, kwargs))
            return handler(method.upper(), url, kwargs)

        monkeypatch.setattr(oauth_storage.requests, "request", fake_request)
        monkeypatch.setattr(oauth_storage.requests, "put", lambda url, **kw: fake_request("PUT", url, **kw))
        monkeypatch.setattr(oauth_storage.requests, "post", lambda url, **kw: fake_request("POST", url, **kw))
        monkeypatch.setattr(oauth_storage.requests, "delete", lambda url, **kw: fake_request("DELETE", url, **kw))
        monkeypatch.setattr(oauth_storage.time, "sleep", lambda s: None)

    def calls_to(self, needle, method=None):
        return [c for c in self.calls if needle in c[1] and (method is None or c[0] == method)]


def _token_response(access="at-1", refresh=None):
    body = {"access_token": access, "expires_in": 3599}
    if refresh:
        body["refresh_token"] = refresh
    return FakeResponse(200, body)


# ---------------------------------------------------------------- tokens

def test_refresh_rotated_onedrive_token_is_persisted_to_the_storage_target(monkeypatch):
    from app import oauth_storage
    from app.database import SessionLocal, init_db
    from app.models import StorageTarget

    init_db()
    with SessionLocal() as db:
        target = StorageTarget(name="od-rotate", type="onedrive", enabled=True,
                               config_json=json.dumps({"refresh_token": "rt-old", "folder_path": "x"}))
        db.add(target)
        db.commit()
        target_id = target.id

    FakeApi(monkeypatch, lambda m, u, kw: _token_response("at-new", refresh="rt-new"))
    config = {"refresh_token": "rt-old"}
    assert oauth_storage._refresh_access_token("onedrive", "rt-old", config) == "at-new"
    assert config["refresh_token"] == "rt-new"

    with SessionLocal() as db:
        stored = json.loads(db.get(StorageTarget, target_id).config_json)
    assert stored["refresh_token"] == "rt-new"
    assert stored["folder_path"] == "x"  # rest of the config untouched


def test_device_flow_token_refreshes_against_common_endpoint_without_secret(monkeypatch):
    from app import oauth_storage

    api = FakeApi(monkeypatch, lambda m, u, kw: _token_response())
    oauth_storage._refresh_access_token("onedrive", "rt", {"refresh_token": "rt", "device_client_id": "public-id"})

    method, url, kwargs = api.calls[0]
    assert url == oauth_storage._MS_DEVICE_TOKEN_URL
    assert kwargs["data"]["client_id"] == "public-id"
    assert "client_secret" not in kwargs["data"]


def test_revoked_refresh_token_gives_actionable_error(monkeypatch):
    from app import oauth_storage

    FakeApi(monkeypatch, lambda m, u, kw: FakeResponse(400, {
        "error": "invalid_grant", "error_description": "Token has been expired or revoked."}))
    with pytest.raises(oauth_storage.OAuthTokenRevoked, match="erneut verbinden"):
        oauth_storage._refresh_access_token("google", "dead", {"refresh_token": "dead"})


def test_requests_are_retried_on_429_with_retry_after(monkeypatch):
    from app import oauth_storage

    answers = [FakeResponse(429, {}, {"Retry-After": "3"}), FakeResponse(503, {}), FakeResponse(200, {"ok": 1})]
    slept = []
    api = FakeApi(monkeypatch, lambda m, u, kw: answers.pop(0))
    monkeypatch.setattr(oauth_storage.time, "sleep", slept.append)

    resp = oauth_storage._request("GET", "https://example.invalid/x")
    assert resp.status_code == 200
    assert len(api.calls) == 3
    assert slept[0] == 3.0  # honoured Retry-After


def test_access_token_is_refreshed_once_on_401(monkeypatch):
    from app import oauth_storage

    state = {"tokens": 0}

    def handler(method, url, kw):
        if "token" in url:
            state["tokens"] += 1
            return _token_response(f"at-{state['tokens']}")
        if kw["headers"]["Authorization"] == "Bearer at-1":
            return FakeResponse(401, {"error": {"code": "InvalidAuthenticationToken"}})
        return FakeResponse(200, {"id": "drive"})

    FakeApi(monkeypatch, handler)
    oauth_storage.check_onedrive_connection({"refresh_token": "rt"})
    assert state["tokens"] == 2


def test_access_token_is_renewed_before_it_expires_during_long_syncs(monkeypatch):
    from app import oauth_storage

    api = FakeApi(monkeypatch, lambda m, u, kw: _token_response())
    clock = [1000.0]
    monkeypatch.setattr(oauth_storage.time, "monotonic", lambda: clock[0])

    token = oauth_storage._AccessToken("google", {"refresh_token": "rt"})
    token.get()
    token.get()
    assert len(api.calls) == 1
    clock[0] += oauth_storage._ACCESS_TOKEN_MAX_AGE + 1
    token.get()
    assert len(api.calls) == 2


# ---------------------------------------------------------------- OAuth callback

def test_callback_rejects_state_that_was_never_issued(monkeypatch):
    from app import oauth_storage

    api = FakeApi(monkeypatch, lambda m, u, kw: _token_response(refresh="rt"))
    with pytest.raises(ValueError):
        oauth_storage.handle_callback("google", "some-code", "forged-state")
    assert api.calls == []  # never even talked to the provider


def test_callback_accepts_issued_state_once(monkeypatch):
    from app import oauth_storage

    monkeypatch.setattr(oauth_storage, "_get_credentials", lambda p: ("cid", "secret"))
    monkeypatch.setattr(oauth_storage, "_require_public_url", lambda: "http://nas:8420")
    oauth_storage._issued_states["good-state"] = {"provider": "google", "created_at": time.time()}

    def handler(method, url, kw):
        if "about" in url:
            return FakeResponse(200, {"user": {"emailAddress": "me@example.com"}})
        return _token_response(refresh="rt-1")

    FakeApi(monkeypatch, handler)
    oauth_storage.handle_callback("google", "code", "good-state")
    assert oauth_storage.pop_pending("good-state")["account"] == "me@example.com"
    with pytest.raises(ValueError):
        oauth_storage.handle_callback("google", "code", "good-state")  # single use


# ---------------------------------------------------------------- OneDrive

def _backup_dir(tmp_path, files):
    from app.config import BACKUPS_DIR
    import uuid

    d = BACKUPS_DIR / f"od-app-{uuid.uuid4().hex[:6]}" / "20250101T000000Z"
    for rel, data in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return d


class FakeGraph:
    """Minimal Microsoft Graph drive: small PUT uploads and upload sessions."""

    def __init__(self, fail_chunk_once_at=None):
        self.files = {}
        self.sessions = {}
        self.fail_chunk_once_at = fail_chunk_once_at

    def __call__(self, method, url, kw):
        if "oauth2" in url:
            return _token_response()
        if url.endswith(":/content") and method == "PUT":
            assert kw["headers"]["Authorization"].startswith("Bearer ")
            path = unquote(url.split("root:/", 1)[1][: -len(":/content")])
            self.files[path] = kw["data"]
            return FakeResponse(201, {"id": path})
        if url.endswith(":/createUploadSession"):
            path = unquote(url.split("root:/", 1)[1][: -len(":/createUploadSession")])
            upload_url = f"https://upload.invalid/{len(self.sessions)}"
            self.sessions[upload_url] = {"path": path, "data": bytearray(), "size": None}
            return FakeResponse(200, {"uploadUrl": upload_url})
        if url in self.sessions:
            session = self.sessions[url]
            if method == "GET":
                return FakeResponse(200, {"nextExpectedRanges": [f"{len(session['data'])}-"]})
            if method == "DELETE":
                return FakeResponse(204)
            # Pre-authenticated URL: sending a bearer token here is a documented error.
            assert "Authorization" not in kw.get("headers", {})
            start_end, size = kw["headers"]["Content-Range"].split(" ")[1].split("/")
            start, end = (int(x) for x in start_end.split("-"))
            chunk_len = end - start + 1
            assert chunk_len == len(kw["data"])
            if end + 1 < int(size):
                assert chunk_len % (320 * 1024) == 0, "Graph requires fragments in multiples of 320 KiB"
            if self.fail_chunk_once_at is not None and start == self.fail_chunk_once_at:
                self.fail_chunk_once_at = None
                session["data"].extend(kw["data"][: 1024])  # partial write, then the connection dies
                raise requests.ConnectionError("connection reset")
            assert start == len(session["data"]), "client must resume exactly where the server is"
            session["data"].extend(kw["data"])
            if len(session["data"]) == int(size):
                self.files[session["path"]] = bytes(session["data"])
                return FakeResponse(201, {"id": session["path"]})
            return FakeResponse(202, {"nextExpectedRanges": [f"{len(session['data'])}-"]})
        if method == "DELETE":
            return FakeResponse(204)
        return FakeResponse(404, {"error": {"code": "itemNotFound"}})


def test_onedrive_sync_uploads_small_and_large_files_with_encoded_paths(monkeypatch, tmp_path):
    from app import oauth_storage

    monkeypatch.setattr(oauth_storage, "_UPLOAD_CHUNK_SIZE", 1280 * 1024)
    big = bytes(range(256)) * (5 * 1024 * 1024 // 256 + 7)
    backup = _backup_dir(tmp_path, {"meta.json": b"{}", "volumes/my vol#1.tar.gz": big})
    graph = FakeGraph()
    FakeApi(monkeypatch, graph)

    oauth_storage.sync_onedrive(backup, {"refresh_token": "rt", "folder_path": "Docker Backups"})

    key = f"{backup.parent.name}/{backup.name}"
    assert graph.files[f"Docker Backups/{key}/meta.json"] == b"{}"
    assert graph.files[f"Docker Backups/{key}/volumes/my vol#1.tar.gz"] == big


def test_onedrive_large_upload_resumes_after_connection_drop(monkeypatch, tmp_path):
    from app import oauth_storage

    chunk = 1280 * 1024
    monkeypatch.setattr(oauth_storage, "_UPLOAD_CHUNK_SIZE", chunk)
    big = b"x" * (5 * 1024 * 1024 + 123)
    backup = _backup_dir(tmp_path, {"data.tar.gz": big})
    graph = FakeGraph(fail_chunk_once_at=2 * chunk)
    FakeApi(monkeypatch, graph)

    oauth_storage.sync_onedrive(backup, {"refresh_token": "rt"})
    assert list(graph.files.values()) == [big]


def test_onedrive_delete_refuses_empty_path_and_tolerates_missing_items(monkeypatch):
    from app import oauth_storage

    api = FakeApi(monkeypatch, FakeGraph())
    with pytest.raises(ValueError):
        oauth_storage.delete_onedrive({"refresh_token": "rt"}, "")
    oauth_storage.delete_onedrive({"refresh_token": "rt", "folder_path": "B"}, "app/2025 01")
    deletes = api.calls_to("graph.microsoft.com", "DELETE")
    assert deletes[0][1].endswith("root:/B/app/2025%2001")


# ---------------------------------------------------------------- Google Drive

class FakeDrive:
    """Minimal Drive v3: folders, files.list query, resumable uploads (308)."""

    def __init__(self, drop_connection_at=None):
        self.items = {}  # id -> {name, parents, folder, data}
        self.sessions = {}
        self.queries = []
        self.drop_connection_at = drop_connection_at

    def _new_id(self):
        return f"id{len(self.items) + 1}"

    def __call__(self, method, url, kw):
        if "oauth2" in url:
            return _token_response()
        if "/upload/" not in url and url.endswith("/drive/v3/files") and method == "GET":
            q = kw["params"]["q"]
            self.queries.append(q)
            name = q.split("name = '", 1)[1].split("' and '", 1)[0].replace("\\'", "'").replace("\\\\", "\\")
            parent = q.split("' and '", 1)[1].split("' in parents", 1)[0]
            want_folder = "mimeType = 'application/vnd.google-apps.folder'" in q
            hits = [{"id": i} for i, it in self.items.items()
                    if it["name"] == name and parent in it["parents"] and it["folder"] == want_folder]
            return FakeResponse(200, {"files": hits})
        if "/upload/" not in url and url.endswith("/drive/v3/files") and method == "POST":
            new_id = self._new_id()
            body = kw["json"]
            self.items[new_id] = {"name": body["name"], "parents": body["parents"], "folder": True, "data": None}
            return FakeResponse(200, {"id": new_id})
        if "/upload/drive/v3/files" in url and method in ("POST", "PATCH"):
            size = int(kw["headers"]["X-Upload-Content-Length"])
            if method == "PATCH":
                file_id = url.rsplit("/", 1)[1]
            else:
                file_id = self._new_id()
                self.items[file_id] = {"name": kw["json"]["name"], "parents": kw["json"]["parents"],
                                       "folder": False, "data": b""}
            session_url = f"https://upload.invalid/{file_id}/{len(self.sessions)}"
            self.sessions[session_url] = {"file_id": file_id, "data": bytearray(), "size": size}
            return FakeResponse(200, {}, {"Location": session_url})
        if url in self.sessions:
            s = self.sessions[url]
            if "Content-Range" not in kw["headers"]:  # single-request upload (empty file)
                self.items[s["file_id"]]["data"] = bytes(kw["data"])
                return FakeResponse(200, {"id": s["file_id"]})
            rng = kw["headers"]["Content-Range"]
            if rng.startswith("bytes */"):
                have = len(s["data"])
                return FakeResponse(308, None, {"Range": f"bytes=0-{have - 1}"} if have else {})
            start_end, size = rng.split(" ")[1].split("/")
            start, end = (int(x) for x in start_end.split("-"))
            if end + 1 < int(size):
                assert (end - start + 1) % (256 * 1024) == 0, "Drive requires chunks in multiples of 256 KiB"
            if self.drop_connection_at is not None and start == self.drop_connection_at:
                self.drop_connection_at = None
                s["data"].extend(kw["data"][:300 * 1024])  # server got part of it
                raise requests.ConnectionError("reset")
            assert start == len(s["data"])
            s["data"].extend(kw["data"])
            if len(s["data"]) == s["size"]:
                self.items[s["file_id"]]["data"] = bytes(s["data"])
                return FakeResponse(200, {"id": s["file_id"]})
            return FakeResponse(308, None, {"Range": f"bytes=0-{len(s['data']) - 1}"})
        if method == "DELETE":
            item_id = url.rsplit("/", 1)[1]
            return FakeResponse(204) if self.items.pop(item_id, None) else FakeResponse(404, {})
        raise AssertionError(f"unexpected call {method} {url}")


def _drive_files(drive):
    return {it["name"]: it["data"] for it in drive.items.values() if not it["folder"]}


def test_google_drive_sync_resumable_upload_resumes_and_does_not_duplicate(monkeypatch, tmp_path):
    from app import oauth_storage

    chunk = 1280 * 1024
    monkeypatch.setattr(oauth_storage, "_UPLOAD_CHUNK_SIZE", chunk)
    big = bytes(range(256)) * (3 * 1024 * 1024 // 256 + 11)
    backup = _backup_dir(tmp_path, {"meta.json": b"{}", "volumes/data.tar.gz": big, "empty.txt": b""})
    drive = FakeDrive(drop_connection_at=chunk)
    FakeApi(monkeypatch, drive)
    config = {"refresh_token": "rt", "folder_path": "Backups/Docker"}

    oauth_storage.sync_google_drive(backup, config)
    assert _drive_files(drive) == {"meta.json": b"{}", "data.tar.gz": big, "empty.txt": b""}

    # Re-syncing the same backup (e.g. a retry) updates instead of duplicating.
    oauth_storage.sync_google_drive(backup, config)
    assert len([it for it in drive.items.values() if not it["folder"]]) == 3
    assert len([it for it in drive.items.values() if it["folder"]]) == 5  # Backups, Docker, app, ts, volumes

    # And the whole version folder can be removed again.
    oauth_storage.delete_google_drive(config, f"{backup.parent.name}/{backup.name}")
    assert not any(it["name"] == backup.name for it in drive.items.values())


def test_google_drive_query_escapes_quotes_in_folder_names(monkeypatch):
    from app import oauth_storage

    drive = FakeDrive()
    FakeApi(monkeypatch, drive)
    oauth_storage.check_google_drive_connection({"refresh_token": "rt", "folder_path": "Thomas' Backups"})
    assert "name = 'Thomas\\' Backups'" in drive.queries[0]
    assert [it["name"] for it in drive.items.values()] == ["Thomas' Backups"]
