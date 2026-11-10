"""Reset (or recreate) the admin login, for when nobody can log in anymore.

Usage (run inside the running container):

    docker exec -it <container> python -m app.reset_password <username> <new-password>

If the username doesn't exist yet, it is created. Requires container/host access,
which is intentional: only someone who already controls the Docker host should be
able to take over the admin account.
"""
import sys

from app.auth import hash_password
from app.database import SessionLocal, init_db
from app.models import User


def reset_password(username: str, new_password: str) -> None:
    if len(username) < 3:
        raise SystemExit("Username must be at least 3 characters")
    if len(new_password) < 8:
        raise SystemExit("Password must be at least 8 characters")

    init_db()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if user:
            user.password_hash = hash_password(new_password)
            user.failed_attempts = 0
            user.locked_until = None
            # This is the recovery path for a lost admin login - make sure the
            # recovered account can actually manage the install again.
            user.is_admin = True
            db.commit()
            print(f"Password for existing user '{username}' has been reset.")
        else:
            user = User(username=username, password_hash=hash_password(new_password), is_admin=True)
            db.add(user)
            db.commit()
            print(f"User '{username}' did not exist and has been created.")
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m app.reset_password <username> <new-password>")
        sys.exit(1)
    reset_password(sys.argv[1], sys.argv[2])
