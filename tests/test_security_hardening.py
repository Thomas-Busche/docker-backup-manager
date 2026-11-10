from fastapi.testclient import TestClient


def _login(client, name):
    from app.reset_password import reset_password

    reset_password(name, "supersecret1")
    r = client.post("/api/auth/login", json={"username": name, "password": "supersecret1"})
    assert r.status_code == 200


def test_cross_site_state_changing_requests_are_blocked():
    from app.main import app

    with TestClient(app) as client:
        _login(client, "csrf-user")
        for site in ("cross-site", "same-site"):
            r = client.post("/api/auth/logout", headers={"Sec-Fetch-Site": site})
            assert r.status_code == 403
        # Still logged in - the forged logout did nothing.
        assert client.get("/api/auth/me").status_code == 200

        # The app's own pages (same-origin) and non-browser clients work as before.
        assert client.get("/api/auth/me", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
        assert client.post("/api/auth/logout", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_security_headers_are_set():
    from app.main import app

    with TestClient(app) as client:
        for path in ("/", "/api/auth/status"):
            r = client.get(path)
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["X-Frame-Options"] == "DENY"
        assert client.get("/api/auth/status").headers["Cache-Control"] == "no-store"


def test_login_with_unknown_user_is_rejected_like_a_wrong_password():
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={"username": "nobody-here", "password": "whatever1"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid credentials"


def test_reset_password_recovers_an_admin_account():
    from app.database import SessionLocal
    from app.models import User
    from app.reset_password import reset_password

    reset_password("recover-me", "supersecret1")
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == "recover-me").first()
        user.is_admin = False
        db.commit()
    reset_password("recover-me", "supersecret2")
    with SessionLocal() as db:
        assert db.query(User).filter(User.username == "recover-me").first().is_admin is True
