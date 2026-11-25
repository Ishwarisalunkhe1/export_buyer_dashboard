"""Personalized outreach with Gmail API support and a labelled demo outbox."""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from config import (
    GMAIL_ACCESS_TOKEN,
    GMAIL_CLIENT_ID,
    GMAIL_CLIENT_SECRET,
    GMAIL_SENDER_EMAIL,
    GMAIL_TOKEN_FILE,
    SAMPLE_PDF_PATH,
    UPLOAD_DIR,
)
from services import normalize_email

DEFAULT_SUBJECT = "Indian {product} supplier for {company}"
DEFAULT_BODY = """Hello {first_name},

I hope you are doing well. We are an India-based export business supplying {product} to buyers, importers, wholesalers, retailers and wellness stores in {country}.

I found {company} while researching businesses that sell related products in {city}, {state}. I would be happy to introduce our product range and discuss supply options for your store or distribution network.

Our product presentation is attached to this email. Please let me know if you would like a quotation or samples.

Best regards,
{sender_name}
{company_name}
"""


def gmail_status() -> dict[str, Any]:
    token = _get_valid_access_token()
    return {
        "connected": bool(token),
        "sender": GMAIL_SENDER_EMAIL or ("Configured token" if token else "Not configured"),
        "mode": "Gmail API" if token else "Demo outbox",
    }


def available_attachments() -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    if SAMPLE_PDF_PATH.exists():
        attachments.append(
            {
                "name": SAMPLE_PDF_PATH.name,
                "path": str(SAMPLE_PDF_PATH),
                "label": "Sample product presentation (demo PDF)",
                "is_demo": True,
            }
        )
    for path in sorted(UPLOAD_DIR.glob("*.pdf"), key=lambda item: item.name):
        attachments.append(
            {
                "name": path.name,
                "path": str(path),
                "label": path.name,
                "is_demo": False,
            }
        )
    return attachments


def template_defaults() -> tuple[str, str]:
    """Return the raw editable template with personalization tokens intact."""
    return DEFAULT_SUBJECT, DEFAULT_BODY


def personalize(template: str, lead: dict[str, Any], product: str, sender_name: str, company_name: str) -> str:
    first_name = str(lead.get("contact_name") or "").split()[0] if lead.get("contact_name") else ""
    company = str(lead.get("company_name") or "your business")
    values = {
        "{first_name}": first_name or "there",
        "{contact_name}": str(lead.get("contact_name") or "there"),
        "{company}": company,
        "{email}": str(lead.get("email") or ""),
        "{product}": product or "singing bowls",
        "{city}": str(lead.get("city") or ""),
        "{state}": str(lead.get("state") or ""),
        "{country}": str(lead.get("country") or ""),
        "{sender_name}": sender_name or "Export Team",
        "{company_name}": company_name or "Our Export Company",
    }
    text = template
    for token, value in values.items():
        text = text.replace(token, value)
    return text


def build_defaults(lead: dict[str, Any], product: str, sender_name: str, company_name: str) -> tuple[str, str]:
    subject = personalize(DEFAULT_SUBJECT, lead, product, sender_name, company_name)
    body = personalize(DEFAULT_BODY, lead, product, sender_name, company_name)
    return subject, body


def _token_payload(token_file: Path) -> dict[str, Any] | None:
    if not token_file.exists():
        return None
    try:
        return json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _token_expired(token: dict[str, Any]) -> bool:
    expiry = token.get("expiry")
    if not expiry:
        return False
    try:
        expiry_dt = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        return expiry_dt <= datetime.now(timezone.utc) + timedelta(minutes=2)
    except ValueError:
        return False


def _get_valid_access_token() -> str:
    if GMAIL_ACCESS_TOKEN:
        return GMAIL_ACCESS_TOKEN

    token = _token_payload(GMAIL_TOKEN_FILE)
    if not token:
        return ""

    access_token = str(token.get("access_token") or "")
    if access_token and not _token_expired(token):
        return access_token

    refresh_token = token.get("refresh_token")
    if not refresh_token or not GMAIL_CLIENT_ID or not GMAIL_CLIENT_SECRET:
        return ""

    data = urllib.parse.urlencode(
        {
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    try:
        request = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            refreshed = json.loads(response.read().decode("utf-8"))
        token["access_token"] = refreshed["access_token"]
        if "expires_in" in refreshed:
            expires = datetime.now(timezone.utc) + timedelta(seconds=int(refreshed["expires_in"]))
            token["expiry"] = expires.isoformat().replace("+00:00", "Z")
        GMAIL_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        GMAIL_TOKEN_FILE.write_text(json.dumps(token, indent=2), encoding="utf-8")
        return str(token["access_token"])
    except Exception:
        return ""


def _build_mime(
    to_email: str, subject: str, body: str, attachment_path: Path | None
) -> str:
    message = MIMEMultipart()
    message["To"] = to_email
    message["From"] = GMAIL_SENDER_EMAIL or "me"
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain", "utf-8"))

    if attachment_path and attachment_path.exists():
        with open(attachment_path, "rb") as handle:
            part = MIMEApplication(handle.read(), _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=attachment_path.name)
        message.attach(part)

    return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")


def send_email(
    lead: dict[str, Any],
    subject: str,
    body: str,
    attachment_path: Path | None = None,
) -> dict[str, str]:
    """Send via Gmail when a valid token exists; otherwise simulate for the UI."""
    to_email = normalize_email(lead.get("email"))
    if not to_email:
        return {
            "status": "failed",
            "provider": "none",
            "error": "Lead has no email address",
        }

    access_token = _get_valid_access_token()
    if not access_token:
        return {
            "status": "demo_sent",
            "provider": "demo-outbox",
            "error": "Demo only: Gmail is not connected, so no real email was sent.",
        }

    try:
        raw = _build_mime(to_email, subject, body, attachment_path)
        payload = json.dumps({"raw": raw}).encode("utf-8")
        request = urllib.request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            data=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        return {
            "status": "sent",
            "provider": "gmail-api",
            "error": f"Gmail message id: {result.get('id', 'unknown')}",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "provider": "gmail-api",
            "error": f"{type(exc).__name__}: {exc}",
        }
