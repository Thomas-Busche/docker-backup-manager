import datetime
from app.clock import utcnow

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import any_user_exists, get_admin_user, get_current_user, hash_password, verify_password
from app.config import LOGIN_LOCKOUT_SECONDS, LOGIN_MAX_ATTEMPTS, SESSION_MAX_AGE
from app.database import get_db
from app.models import User
from fastapi import Request

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SetupPayload(BaseModel):
    username: str
    password: str


class LoginPayload(BaseModel):
    username: str
    password: str


@router.get("/status")
def status(db: Session = Depends(get_db)):
    return {"setup_required": not any_user_exists(db)}


@router.post("/setup")
def setup(payload: SetupPayload, request: Request, db: Session = Depends(get_db)):
    if any_user_exists(db):
        raise HTTPException(400, "Setup already completed")
    if len(payload.username) < 3 or len(payload.password) < 8:
        raise HTTPException(400, "Username min 3 chars, password min 8 chars")
    user = User(username=payload.username, password_hash=hash_password(payload.password), is_admin=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    request.session["user_id"] = user.id
    return {"ok": True}


# Verified against when the username doesn't exist, so a wrong username takes
# as long as a wrong password and can't be used to discover valid accounts.
_DUMMY_HASH = hash_password("dbm-timing-equalizer")


@router.post("/login")
def login(payload: LoginPayload, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username).first()

    if user and user.locked_until and user.locked_until > utcnow():
        raise HTTPException(429, "Too many failed attempts, try again later")

    if not user:
        verify_password(payload.password, _DUMMY_HASH)
    if not user or not verify_password(payload.password, user.password_hash):
        if user:
            user.failed_attempts += 1
            if user.failed_attempts >= LOGIN_MAX_ATTEMPTS:
                user.locked_until = utcnow() + datetime.timedelta(seconds=LOGIN_LOCKOUT_SECONDS)
                user.failed_attempts = 0
            db.commit()
        raise HTTPException(401, "Invalid credentials")

    user.failed_attempts = 0
    user.locked_until = None
    db.commit()
    # Start from a clean session (no fixation of anything set before login).
    request.session.clear()
    request.session["user_id"] = user.id
    return {"ok": True}


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {
        "username": user.username,
        "is_admin": user.is_admin,
        "session_max_age_hours": round(SESSION_MAX_AGE / 3600, 1),
    }


class PasswordChangePayload(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
def change_password(payload: PasswordChangePayload, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(400, "Current password incorrect")
    if len(payload.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}


# ---------- User management (admin only) ----------

class CreateUserPayload(BaseModel):
    username: str
    password: str
    is_admin: bool = False


@router.get("/users")
def list_users(admin: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.created_at).all()
    return {"users": [
        {
            "id": u.id,
            "username": u.username,
            "is_admin": u.is_admin,
            "created_at": u.created_at.isoformat() + "Z",
            "locked": bool(u.locked_until and u.locked_until > utcnow()),
        }
        for u in users
    ]}


@router.post("/users")
def create_user(payload: CreateUserPayload, admin: User = Depends(get_admin_user),
                db: Session = Depends(get_db)):
    if len(payload.username) < 3 or len(payload.password) < 8:
        raise HTTPException(400, "Benutzername mind. 3, Passwort mind. 8 Zeichen")
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(409, "Benutzername bereits vergeben")
    user = User(username=payload.username, password_hash=hash_password(payload.password),
                is_admin=payload.is_admin)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    if user_id == admin.id:
        raise HTTPException(400, "Eigenen Account kann man nicht löschen")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(404, "Benutzer nicht gefunden")
    # Sicherstellen dass mindestens ein Admin übrig bleibt
    if target.is_admin:
        other_admins = db.query(User).filter(User.is_admin == True, User.id != user_id).count()  # noqa: E712
        if other_admins == 0:
            raise HTTPException(400, "Letzten Admin kann man nicht löschen")
    db.delete(target)
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/unlock")
def unlock_user(user_id: int, admin: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(404, "Benutzer nicht gefunden")
    target.failed_attempts = 0
    target.locked_until = None
    db.commit()
    return {"ok": True}
