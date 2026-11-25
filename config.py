"""Configuration for the export buyer discovery dashboard.

Real API integrations are optional. When credentials are absent the app runs in
clearly labelled demo mode so the complete workflow remains testable.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
SAMPLE_PDF_PATH = BASE_DIR / "assets" / "sample_singing_bowls_presentation.pdf"

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
(BASE_DIR / "assets").mkdir(exist_ok=True)

DB_PATH = Path(os.getenv("EXPORT_DASHBOARD_DB", str(DATA_DIR / "export_buyers.db")))
SECRET_KEY = os.getenv("EXPORT_DASHBOARD_SECRET", "dev-only-change-this-secret")

# Gemini AI classification (requirement from the video).
# gemini-3.5-flash-lite is a currently supported stable, cost-efficient model.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "20"))

# Gmail outreach (requirement from the video).
GMAIL_ACCESS_TOKEN = os.getenv("GMAIL_ACCESS_TOKEN", "")
GMAIL_SENDER_EMAIL = os.getenv("GMAIL_SENDER_EMAIL", "")
GMAIL_TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", str(BASE_DIR / "credentials" / "gmail_token.json")))
GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")

# Anti-spam controls. The video explicitly warns against spamming buyers.
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", "10"))
MAX_EMAILS_PER_LEAD_PER_DAY = int(os.getenv("MAX_EMAILS_PER_LEAD_PER_DAY", "1"))

# "format" is an honest syntax check. "mx" additionally uses dnspython when available.
EMAIL_VALIDATION_MODE = os.getenv("EMAIL_VALIDATION_MODE", "format").lower()

DEFAULT_PRODUCT = os.getenv("DEFAULT_PRODUCT", "singing bowls")
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY", "United States")
DEFAULT_STATE = os.getenv("DEFAULT_STATE", "Texas")
DEFAULT_CITY = os.getenv("DEFAULT_CITY", "Houston")

DEMO_SEED = os.getenv("EXPORT_DASHBOARD_SEED", "1") != "0"
