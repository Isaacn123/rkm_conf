import logging
import os
from datetime import datetime, timezone
from typing import Any

import mysql.connector
import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field


MYSQL_HOST = os.getenv("MYSQL_HOST", "mysql")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DB = os.getenv("MYSQL_DB", "rkmconf")
MYSQL_USER = os.getenv("MYSQL_USER", "rkmconf")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "rkmconf_password")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "")
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "RKMConf")
ADMIN_NOTIFY_EMAIL = os.getenv("ADMIN_NOTIFY_EMAIL", "church@robertkayanjaministries.ug")

logger = logging.getLogger(__name__)


def _brevo_send_email(*, to_email: str, to_name: str, subject: str, html: str) -> bool:
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL:
        return False

    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": BREVO_API_KEY,
            "accept": "application/json",
            "content-type": "application/json",
        },
        json={
            "sender": {"email": BREVO_SENDER_EMAIL, "name": BREVO_SENDER_NAME},
            "to": [{"email": to_email, "name": to_name}],
            "subject": subject,
            "htmlContent": html,
        },
        timeout=15,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Brevo send failed: {resp.status_code}")
    return True


def _format_admin_email(*, payload: "FormSubmission") -> str:
    invitation = "Yes" if payload.invitationLetter == "yes" else "No"
    passport = (payload.passport or "").strip()
    passport_html = passport if passport else "<em>(not provided)</em>"
    return f"""
      <div style="font-family:Arial,sans-serif;line-height:1.5">
        <h2>New RKMConf form submission</h2>
        <p><strong>Name:</strong> {payload.fname.strip()} {payload.lname.strip()}</p>
        <p><strong>Email:</strong> {payload.email.strip().lower()}</p>
        <p><strong>Phone:</strong> {payload.phoneCountry.strip().upper()} {payload.phone.strip()}</p>
        <p><strong>Days in attendance:</strong> {payload.daysAttendance.strip()}</p>
        <p><strong>Passport:</strong> {passport_html}</p>
        <p><strong>Invitation letter required:</strong> {invitation}</p>
        <p><strong>Message:</strong><br />{payload.message.strip().replace("\n", "<br />")}</p>
      </div>
    """


def _format_visitor_email(*, payload: "FormSubmission") -> str:
    invitation = "Yes" if payload.invitationLetter == "yes" else "No"
    return f"""
      <div style="font-family:Arial,sans-serif;line-height:1.5">
        <p>Hello {payload.fname.strip()},</p>
        <p>Thank you for registering for RKMConf. We’ve received your submission.</p>
        <h3>Summary</h3>
        <ul>
          <li><strong>Name:</strong> {payload.fname.strip()} {payload.lname.strip()}</li>
          <li><strong>Email:</strong> {payload.email.strip().lower()}</li>
          <li><strong>Phone:</strong> {payload.phoneCountry.strip().upper()} {payload.phone.strip()}</li>
          <li><strong>Days in attendance:</strong> {payload.daysAttendance.strip()}</li>
          <li><strong>Invitation letter required:</strong> {invitation}</li>
        </ul>
        <p>If you need to update any details, please reply to this email.</p>
      </div>
    """


def get_conn() -> mysql.connector.MySQLConnection:
    return mysql.connector.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        autocommit=True,
    )


def init_db() -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              created_at DATETIME(6) NOT NULL,
              fname VARCHAR(100) NOT NULL,
              lname VARCHAR(100) NOT NULL,
              email VARCHAR(320) NOT NULL,
              passport VARCHAR(32) NULL,
              invitation_letter TINYINT(1) NOT NULL DEFAULT 0,
              phone_country CHAR(2) NOT NULL,
              phone VARCHAR(40) NOT NULL,
              message TEXT NOT NULL,
              days_attendance VARCHAR(20) NOT NULL,
              PRIMARY KEY (id),
              INDEX idx_created_at (created_at),
              INDEX idx_email (email)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """
        )

        # Lightweight migration for existing DBs
        cur.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'submissions'
            """,
            (MYSQL_DB,),
        )
        existing = {row[0] for row in cur.fetchall()}
        if "passport" not in existing:
            cur.execute("ALTER TABLE submissions ADD COLUMN passport VARCHAR(32) NULL;")
        if "invitation_letter" not in existing:
            cur.execute(
                "ALTER TABLE submissions ADD COLUMN invitation_letter TINYINT(1) NOT NULL DEFAULT 0;"
            )
        cur.close()


class FormSubmission(BaseModel):
    fname: str = Field(min_length=1, max_length=100)
    lname: str = Field(min_length=1, max_length=100)
    email: EmailStr
    passport: str | None = Field(default=None, max_length=32)
    invitationLetter: str | None = Field(default=None, pattern="^(yes|no)$")
    phoneCountry: str = Field(min_length=2, max_length=2, description="ISO 2 country code")
    phone: str = Field(min_length=3, max_length=40)
    message: str = Field(min_length=1, max_length=5000)
    daysAttendance: str = Field(min_length=1, max_length=20)


app = FastAPI(title="RKMConf Form API")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/form/submit")
def submit(payload: FormSubmission) -> dict[str, Any]:
    try:
        created_at = datetime.now(timezone.utc).replace(tzinfo=None)
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO submissions
                (created_at, fname, lname, email, passport, invitation_letter, phone_country, phone, message, days_attendance)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    created_at,
                    payload.fname.strip(),
                    payload.lname.strip(),
                    payload.email.strip().lower(),
                    (payload.passport.strip() if payload.passport else None),
                    (1 if payload.invitationLetter == "yes" else 0),
                    payload.phoneCountry.strip().upper(),
                    payload.phone.strip(),
                    payload.message.strip(),
                    payload.daysAttendance.strip(),
                ),
            )
            cur.close()

        # Transactional email (Brevo). Misconfiguration or API errors must not roll back DB insert.
        confirmation_email_sent = False
        if not BREVO_API_KEY or not BREVO_SENDER_EMAIL:
            logger.warning(
                "Brevo not configured (set BREVO_API_KEY and BREVO_SENDER_EMAIL); skipping emails"
            )
        else:
            if ADMIN_NOTIFY_EMAIL:
                try:
                    _brevo_send_email(
                        to_email=ADMIN_NOTIFY_EMAIL,
                        to_name="Admin",
                        subject="New RKMConf submission",
                        html=_format_admin_email(payload=payload),
                    )
                except Exception:
                    logger.exception("Admin notification email failed")
            try:
                confirmation_email_sent = _brevo_send_email(
                    to_email=payload.email.strip().lower(),
                    to_name=f"{payload.fname.strip()} {payload.lname.strip()}".strip(),
                    subject="RKMConf: We received your submission",
                    html=_format_visitor_email(payload=payload),
                )
            except Exception:
                logger.exception("Visitor confirmation email failed")

        return {"ok": True, "confirmation_email_sent": confirmation_email_sent}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to save submission") from e


@app.get("/api/form/submissions")
def list_submissions(
    limit: int = 100,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN not configured")
    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, created_at, fname, lname, email, passport, invitation_letter, phone_country, phone, message, days_attendance
            FROM submissions
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        cur.close()
    return {"ok": True, "items": rows}

