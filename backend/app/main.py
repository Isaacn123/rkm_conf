import logging
import os
from datetime import datetime, timezone
from html import escape
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


def _escape_line(s: str) -> str:
    return escape((s or "").strip(), quote=False)


def _escape_multiline(s: str) -> str:
    return escape((s or "").strip(), quote=False).replace("\n", "<br />")


def _format_utc_timestamp(dt: datetime) -> str:
    """Human-readable UTC label for naive UTC datetimes stored from submit()."""
    return dt.strftime("%d %B %Y, %H:%M UTC")


def _email_layout(*, header_subtitle: str, body_html: str, footer_html: str) -> str:
    sub = escape(header_subtitle, quote=False)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width"/></head>
<body style="margin:0;padding:0;background-color:#f3f4f6;-webkit-font-smoothing:antialiased;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color:#f3f4f6;padding:28px 14px;">
  <tr>
    <td align="center">
      <table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;border:1px solid #e5e7eb;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06);">
        <tr>
          <td style="background:#1e293b;padding:22px 28px;border-bottom:3px solid #0f172a;">
            <p style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:21px;font-weight:600;color:#f8fafc;letter-spacing:.02em;">Robert Kayanja Ministries Conference(THE BIG FIX)</p>
            <p style="margin:10px 0 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#cbd5e1;line-height:1.45;">{sub}</p>
          </td>
        </tr>
        <tr>
          <td style="padding:28px 28px 8px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#111827;">
            {body_html}
          </td>
        </tr>
        <tr>
          <td style="padding:18px 28px 26px;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.55;color:#6b7280;border-top:1px solid #f3f4f6;background:#fafafa;">
            {footer_html}
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def _detail_row(label: str, value_html: str) -> str:
    lab = escape(label, quote=False)
    return f"""<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:0 0 12px;">
  <tr>
    <td style="padding:10px 0 10px 0;border-bottom:1px solid #eef0f3;font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.04em;width:38%;vertical-align:top;">{lab}</td>
    <td style="padding:10px 0 10px 16px;border-bottom:1px solid #eef0f3;font-size:14px;color:#111827;vertical-align:top;">{value_html}</td>
  </tr>
</table>"""


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


def _format_admin_email(*, payload: "FormSubmission", submitted_at_utc: datetime) -> str:
    invitation = "Yes" if payload.invitationLetter == "yes" else "No"
    passport_raw = (payload.passport or "").strip()
    passport_html = _escape_line(passport_raw) if passport_raw else '<span style="color:#94a3b8;font-style:italic;">Not provided</span>'

    phone_disp = f"{_escape_line(payload.phoneCountry.strip().upper())} {_escape_line(payload.phone.strip())}"
    full_name = f"{_escape_line(payload.fname)} {_escape_line(payload.lname)}"
    email_raw = payload.email.strip().lower()
    email_disp = _escape_line(email_raw)
    days_disp = _escape_line(payload.daysAttendance.strip())
    msg_html = _escape_multiline(payload.message)
    ts = escape(_format_utc_timestamp(submitted_at_utc), quote=False)

    details = "".join(
        [
            _detail_row("Received (UTC)", ts),
            _detail_row("Full name", full_name),
            _detail_row(
                "Email",
                f'<a href="mailto:{email_raw}" style="color:#1d4ed8;text-decoration:none;">{email_disp}</a>',
            ),
            _detail_row("Phone", phone_disp),
            _detail_row("Days attending", days_disp),
            _detail_row("Passport number", passport_html),
            _detail_row("Invitation letter", escape(invitation, quote=False)),
        ]
    )

    body = f"""
<p style="margin:0 0 20px;font-size:15px;color:#111827;">
  A new registration has been submitted through the Robert Kayanja Ministries Conference website form. Please review the details below and follow up as appropriate.
</p>
{details}
<p style="margin:22px 0 8px;font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.04em;">Additional message</p>
<div style="margin:0;padding:14px 16px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;font-size:14px;color:#1e293b;line-height:1.6;">
  {msg_html}
</div>
"""

    footer = """
<p style="margin:0 0 8px;">This is an automated notice sent to the conference administration mailbox.</p>
<p style="margin:0;">Please do not forward externally if the contents include personal data.</p>
"""
    return _email_layout(
        header_subtitle="New registration — internal notice",
        body_html=body,
        footer_html=footer,
    )


def _format_visitor_email(*, payload: "FormSubmission", submitted_at_utc: datetime) -> str:
    invitation = "Yes" if payload.invitationLetter == "yes" else "No"
    first = _escape_line(payload.fname)
    full_name = f"{_escape_line(payload.fname)} {_escape_line(payload.lname)}"
    email_disp = _escape_line(payload.email.strip().lower())
    phone_disp = f"{_escape_line(payload.phoneCountry.strip().upper())} {_escape_line(payload.phone.strip())}"
    days_disp = _escape_line(payload.daysAttendance.strip())

    details = "".join(
        [
            _detail_row("Name", full_name),
            _detail_row("Email", email_disp),
            _detail_row("Phone", phone_disp),
            _detail_row("Days attending", days_disp),
            _detail_row("Invitation letter requested", escape(invitation, quote=False)),
        ]
    )

    body = f"""
<p style="margin:0 0 14px;font-size:15px;color:#111827;">Dear {first},</p>
<p style="margin:0 0 16px;font-size:15px;color:#374151;">
  Thank you for submitting your registration details for <strong style="color:#111827;">Robert Kayanja Ministries Conference(THE BIG FIX)</strong>.
  We have received your information securely and will retain it for conference planning and correspondence.
</p>
<p style="margin:0 0 22px;font-size:15px;color:#374151;">
  If any of the details below need to be corrected, please reply to this email and our team will assist you.
</p>
<p style="margin:0 0 12px;font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.04em;">Your submission summary</p>
{details}
<p style="margin:22px 0 0;font-size:13px;color:#64748b;line-height:1.55;">
  Submitted: {_escape_line(_format_utc_timestamp(submitted_at_utc))}
</p>
<p style="margin:24px 0 0;font-size:15px;color:#111827;">
  Kind regards,<br/>
  <span style="color:#374151;">The Robert Kayanja Ministries Conference team</span>
</p>
"""

    footer = """
<p style="margin:0 0 8px;">You received this email because you completed the registration form on the Robert Kayanja Ministries Conference website.</p>
<p style="margin:0;">If you did not submit this request, please disregard this message or contact us using the details on our official site.</p>
"""
    return _email_layout(
        header_subtitle="Registration confirmation",
        body_html=body,
        footer_html=footer,
    )


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
                        subject="Robert Kayanja Ministries Conference — New registration submitted",
                        html=_format_admin_email(payload=payload, submitted_at_utc=created_at),
                    )
                except Exception:
                    logger.exception("Admin notification email failed")
            try:
                confirmation_email_sent = _brevo_send_email(
                    to_email=payload.email.strip().lower(),
                    to_name=f"{payload.fname.strip()} {payload.lname.strip()}".strip(),
                    subject="Robert Kaynja Ministries Conference — Registration received",
                    html=_format_visitor_email(payload=payload, submitted_at_utc=created_at),
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

