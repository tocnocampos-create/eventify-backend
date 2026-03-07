"""Seed script to create initial admin user."""
from app.core.security import get_password_hash
from app.db.base import SessionLocal
from app.db.models import User, UserRole


def seed_admin():
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == "admin@eventify.com").first()
        if existing:
            print("Admin user already exists, skipping.")
            return
        admin = User(
            email="admin@eventify.com",
            password_hash=get_password_hash("admin123"),
            full_name="Admin",
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(admin)
        db.commit()
        print("Admin user created: admin@eventify.com / admin123")
    finally:
        db.close()


if __name__ == "__main__":
    seed_admin()
