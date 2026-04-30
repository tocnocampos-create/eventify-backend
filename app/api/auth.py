"""Authentication API endpoints."""
import logging
import os
import random
import string
from datetime import datetime, timedelta, timezone

import resend
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

resend.api_key = os.environ.get("RESEND_API_KEY", "")

from app.api.dependencies import get_current_user
from app.core.security import create_access_token, create_refresh_token, decode_token, get_password_hash
from app.db.base import get_db
from app.db.models import PasswordResetCode, User
from app.models.schemas import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    RefreshRequest,
    ResetPasswordRequest,
    ResetPasswordResponse,
    Token,
    UserCreate,
    UserResponse,
)
from app.services.user_service import UserService
from app.services.user_preferences_service import UserPreferencesService

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(user_data: UserCreate, db: Session = Depends(get_db)):
    """Register a new user."""
    existing = UserService.get_user_by_email(db, user_data.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )
    user = UserService.create_user(db, user_data)
    return user


@router.post("/login", response_model=Token)
async def login(login_data: LoginRequest, db: Session = Depends(get_db)):
    """Login with email and password."""
    user = UserService.authenticate_user(db, login_data.email, login_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        )
    return Token(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
        token_type="bearer",
    )


@router.post("/refresh", response_model=Token)
async def refresh(refresh_data: RefreshRequest, db: Session = Depends(get_db)):
    """Get new token pair using a refresh token."""
    payload = decode_token(refresh_data.refresh_token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )
    if payload.get("token_type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type, refresh token required",
        )
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    user = UserService.get_user(db, int(user_id))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return Token(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
        token_type="bearer",
    )


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
async def forgot_password(body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Generate a 6-digit reset code valid for 15 minutes and send it by email."""
    user = UserService.get_user_by_email(db, body.email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with that email",
        )

    code = "".join(random.choices(string.digits, k=6))
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)

    reset_code = PasswordResetCode(
        email=body.email.lower(),
        code=code,
        expires_at=expires_at,
    )
    db.add(reset_code)
    db.commit()

    try:
        resend.Emails.send({
            "from": "Eventify <noreply@eventifyapp.cl>",
            "to": [body.email],
            "subject": "Código de recuperación de contraseña",
            "html": f"""
<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"></head>
<body style="font-family:sans-serif;background:#f4f4f4;margin:0;padding:0;">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 0;">
      <table width="480" cellpadding="0" cellspacing="0"
             style="background:#1a1a2e;border-radius:12px;overflow:hidden;">
        <tr>
          <td style="background:#9B5DE5;padding:24px 32px;">
            <h1 style="color:#fff;margin:0;font-size:22px;font-weight:700;">
              🎟 Eventify
            </h1>
          </td>
        </tr>
        <tr>
          <td style="padding:32px;">
            <p style="color:#e0e0e0;font-size:16px;margin:0 0 24px;">
              Recibimos una solicitud para restablecer la contraseña de tu cuenta.
            </p>
            <p style="color:#b0b0b0;font-size:14px;margin:0 0 16px;">
              Tu código de recuperación de Eventify es:
            </p>
            <div style="background:#2a2a4a;border:2px solid #9B5DE5;border-radius:8px;
                        text-align:center;padding:20px;margin:0 0 24px;">
              <span style="color:#BFA0FF;font-size:36px;font-weight:700;
                           letter-spacing:12px;">{code}</span>
            </div>
            <p style="color:#b0b0b0;font-size:13px;margin:0 0 8px;">
              Este código expira en <strong style="color:#e0e0e0;">15 minutos</strong>.
            </p>
            <p style="color:#707070;font-size:12px;margin:0;">
              Si no solicitaste este código, puedes ignorar este mensaje.
              Tu contraseña no será cambiada.
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 32px;border-top:1px solid #2a2a4a;">
            <p style="color:#505050;font-size:11px;margin:0;text-align:center;">
              © 2026 Eventify · Santiago, Chile
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
""",
        })
    except Exception as exc:
        logger.error("Failed to send password reset email to %s: %s", body.email, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo enviar el email. Intenta de nuevo.",
        )

    return ForgotPasswordResponse(message="Código enviado a tu email")


@router.post("/reset-password", response_model=ResetPasswordResponse)
async def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Validate the reset code and update the user's password."""
    now = datetime.now(timezone.utc)

    reset_code = (
        db.query(PasswordResetCode)
        .filter(
            PasswordResetCode.email == body.email.lower(),
            PasswordResetCode.code == body.code,
            PasswordResetCode.used == False,  # noqa: E712
            PasswordResetCode.expires_at > now,
        )
        .order_by(PasswordResetCode.created_at.desc())
        .first()
    )

    if not reset_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset code",
        )

    user = UserService.get_user_by_email(db, body.email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    user.password_hash = get_password_hash(body.new_password)
    reset_code.used = True
    db.commit()

    return ResetPasswordResponse(message="Password updated")


@router.get("/me", response_model=UserResponse)
async def get_me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get current user profile."""
    has_interests = UserPreferencesService.has_interests(db, current_user.id)
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role.value if hasattr(current_user.role, 'value') else current_user.role,
        is_active=current_user.is_active,
        has_interests=has_interests,
        created_at=current_user.created_at,
    )
