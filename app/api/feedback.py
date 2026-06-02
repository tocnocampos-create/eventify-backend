"""Feedback submission endpoint."""
import logging
import os

import resend
from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.db.base import get_db
from app.db.models import User

logger = logging.getLogger(__name__)

resend.api_key = os.environ.get("RESEND_API_KEY", "")

_FEEDBACK_TO = os.environ.get("FEEDBACK_EMAIL", "tocnocampos@gmail.com")

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackSchema(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must not be empty")
        return v.strip()


@router.post("")
async def submit_feedback(
    feedback: FeedbackSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sender_name = current_user.full_name or current_user.email
    try:
        resend.Emails.send({
            "from": "Eventify <noreply@eventifyapp.cl>",
            "to": _FEEDBACK_TO,
            "subject": f"Feedback Beta — {sender_name}",
            "html": (
                f"<h3>Nuevo feedback de {sender_name}</h3>"
                f"<p><strong>Usuario:</strong> {current_user.email}</p>"
                f"<p><strong>Mensaje:</strong></p>"
                f"<p>{feedback.message}</p>"
            ),
        })
    except Exception:
        logger.exception("Resend failed for feedback from user %s", current_user.id)
        # Don't surface internal errors to the client
    return {"ok": True}
