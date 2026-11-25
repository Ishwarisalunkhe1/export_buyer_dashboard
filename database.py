"""SQLite persistence and duplicate-safe lead operations."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from config import DB_PATH, DEMO_SEED
from services import (
    DEMO_LEADS,
    classify_buyer,
    normalize_company,
    normalize_email,
    normalize_website,
    now_iso,
    validate_email_address,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_name TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    email_normalized TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    website_normalized TEXT NOT NULL DEFAULT '',
    company_normalized TEXT NOT NULL DEFAULT '',
    product TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    buyer_type TEXT NOT NULL DEFAULT 'unclassified',
    classification_reason TEXT NOT NULL DEFAULT '',
    classification_method TEXT NOT NULL DEFAULT '',
    classification_confidence REAL,
    classification_at TEXT NOT NULL DEFAULT '',
    email_valid INTEGER NOT NULL DEFAULT 0,
    email_validation_note TEXT NOT NULL DEFAULT '',
    is_demo INTEGER NOT NULL DEFAULT 0,
    last_contacted_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    lead_name TEXT NOT NULL DEFAULT '',
    lead_email TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    attachment_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    total_rows INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    invalid INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    sources TEXT NOT NULL DEFAULT '',
    found INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email_normalized);
CREATE INDEX IF NOT EXISTS idx_leads_website ON leads(website_normalized);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_normalized);
CREATE INDEX IF NOT EXISTS idx_history_lead ON email_history(lead_id);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate_db(conn: sqlite3.Connection) -> None:
    """Add newer classification columns without dropping existing lead data."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    if "classification_confidence" not in columns:
        conn.execute("ALTER TABLE leads ADD COLUMN classification_confidence REAL")
    if "classification_at" not in columns:
        conn.execute("ALTER TABLE leads ADD COLUMN classification_at TEXT NOT NULL DEFAULT ''")


def init_db(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        migrate_db(conn)
        if DEMO_SEED and conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0:
            for lead in DEMO_LEADS:
                insert_lead(conn, lead, is_demo=True)


def _duplicate_reason(conn: sqlite3.Connection, lead: dict[str, Any], exclude_id: int | None = None) -> str:
    email_n = normalize_email(lead.get("email"))
    website_n = normalize_website(lead.get("website"))
    company_n = normalize_company(lead.get("company_name"))
    exclude_clause = " AND id != ?" if exclude_id else ""
    exclude_args: tuple[Any, ...] = (exclude_id,) if exclude_id else ()

    if email_n:
        row = conn.execute(
            f"SELECT id FROM leads WHERE email_normalized = ?{exclude_clause} LIMIT 1",
            (email_n, *exclude_args),
        ).fetchone()
        if row:
            return "duplicate_email"

    if website_n:
        row = conn.execute(
            f"SELECT id FROM leads WHERE website_normalized = ?{exclude_clause} LIMIT 1",
            (website_n, *exclude_args),
        ).fetchone()
        if row:
            return "duplicate_website"

    if company_n:
        row = conn.execute(
            f"SELECT id FROM leads WHERE company_normalized = ? "
            f"AND LOWER(COALESCE(city, '')) = LOWER(?) "
            f"AND LOWER(COALESCE(country, '')) = LOWER(?){exclude_clause} LIMIT 1",
            (company_n, lead.get("city", ""), lead.get("country", ""), *exclude_args),
        ).fetchone()
        if row:
            return "duplicate_company"

    return ""


def insert_lead(
    conn: sqlite3.Connection,
    lead: dict[str, Any],
    source_url: str = "",
    is_demo: bool = False,
    allow_duplicates: bool = False,
) -> tuple[str, int | None]:
    """Insert a lead after email/website/company duplicate checks."""
    email = str(lead.get("email") or "").strip()
    phone = str(lead.get("phone") or "").strip()
    website = str(lead.get("website") or "").strip()
    company = str(lead.get("company_name") or "").strip()
    contact = str(lead.get("contact_name") or "").strip()

    if not any([email, phone, website, company, contact]):
        return "invalid", None

    if not allow_duplicates:
        reason = _duplicate_reason(conn, lead)
        if reason:
            return reason, None

    validation = validate_email_address(email)
    timestamp = now_iso()
    cursor = conn.execute(
        """
        INSERT INTO leads (
            contact_name, company_name, email, email_normalized, phone,
            website, website_normalized, company_normalized, product, country,
            state, city, source, source_url, role, buyer_type,
            classification_reason, classification_method, classification_confidence,
            classification_at, email_valid, email_validation_note, is_demo,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contact,
            company,
            email,
            normalize_email(email),
            phone,
            website,
            normalize_website(website),
            normalize_company(company),
            str(lead.get("product") or "").strip(),
            str(lead.get("country") or "").strip(),
            str(lead.get("state") or "").strip(),
            str(lead.get("city") or "").strip(),
            str(lead.get("source") or "").strip(),
            source_url or str(lead.get("source_url") or "").strip(),
            str(lead.get("role") or "").strip(),
            str(lead.get("buyer_type") or "unclassified").strip() or "unclassified",
            str(lead.get("classification_reason") or "").strip(),
            str(lead.get("classification_method") or "").strip(),
            lead.get("classification_confidence"),
            str(lead.get("classification_at") or "").strip(),
            1 if validation["valid"] else 0,
            validation["reason"],
            1 if is_demo else 0,
            timestamp,
            timestamp,
        ),
    )
    return "inserted", int(cursor.lastrowid)


def get_leads(conn: sqlite3.Connection, filters: dict[str, Any] | None = None) -> list[sqlite3.Row]:
    filters = filters or {}
    clauses: list[str] = []
    args: list[Any] = []

    search = str(filters.get("q") or "").strip()
    if search:
        clauses.append(
            "(LOWER(contact_name) LIKE ? OR LOWER(company_name) LIKE ? OR LOWER(email) LIKE ? "
            "OR LOWER(website) LIKE ? OR LOWER(city) LIKE ? OR LOWER(product) LIKE ?)"
        )
        pattern = f"%{search.lower()}%"
        args.extend([pattern] * 6)

    for field in ("product", "country", "state", "city", "source", "buyer_type"):
        value = str(filters.get(field) or "").strip()
        if value:
            clauses.append(f"LOWER({field}) = LOWER(?)")
            args.append(value)

    validation = str(filters.get("validation") or "").strip()
    if validation == "valid":
        clauses.append("email_valid = 1")
    elif validation == "invalid":
        clauses.append("email_valid = 0")

    if str(filters.get("demo") or "") == "0":
        clauses.append("is_demo = 0")

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return conn.execute(
        f"SELECT * FROM leads{where} ORDER BY created_at DESC, id DESC", args
    ).fetchall()


def get_lead(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


def update_lead(conn: sqlite3.Connection, lead_id: int, values: dict[str, Any]) -> tuple[bool, str]:
    existing = get_lead(conn, lead_id)
    if not existing:
        return False, "Lead not found"

    candidate = {**dict(existing), **values}
    reason = _duplicate_reason(conn, candidate, exclude_id=lead_id)
    if reason:
        return False, f"Update blocked: {reason.replace('duplicate_', 'duplicate ')}"

    validation = validate_email_address(str(values.get("email", existing["email"]) or ""))
    fields = [
        "contact_name", "company_name", "email", "phone", "website", "product",
        "country", "state", "city", "source", "role",
    ]
    sets = [f"{field} = ?" for field in fields]
    args: list[Any] = [str(values.get(field, existing[field]) or "") for field in fields]
    sets.extend(
        [
            "email_normalized = ?", "website_normalized = ?", "company_normalized = ?",
            "email_valid = ?", "email_validation_note = ?", "updated_at = ?",
        ]
    )
    args.extend(
        [
            normalize_email(candidate.get("email")),
            normalize_website(candidate.get("website")),
            normalize_company(candidate.get("company_name")),
            1 if validation["valid"] else 0,
            validation["reason"],
            now_iso(),
        ]
    )
    args.append(lead_id)
    conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", args)
    return True, "Lead updated"


def delete_leads(conn: sqlite3.Connection, ids: Iterable[int]) -> int:
    id_list = [int(value) for value in ids]
    if not id_list:
        return 0
    placeholders = ",".join("?" for _ in id_list)
    cursor = conn.execute(f"DELETE FROM leads WHERE id IN ({placeholders})", id_list)
    return cursor.rowcount


def validate_leads(conn: sqlite3.Connection, ids: Iterable[int]) -> tuple[int, int]:
    valid_count = 0
    invalid_count = 0
    for lead_id in ids:
        row = get_lead(conn, int(lead_id))
        if not row:
            continue
        result = validate_email_address(row["email"])
        conn.execute(
            "UPDATE leads SET email_valid = ?, email_validation_note = ?, updated_at = ? WHERE id = ?",
            (1 if result["valid"] else 0, result["reason"], now_iso(), lead_id),
        )
        if result["valid"]:
            valid_count += 1
        else:
            invalid_count += 1
    return valid_count, invalid_count


def classify_leads(
    conn: sqlite3.Connection, ids: Iterable[int], force_demo: bool = False
) -> dict[str, Any]:
    counts: dict[str, int] = {"business": 0, "individual": 0, "uncertain": 0}
    methods: dict[str, int] = {}
    for lead_id in ids:
        row = get_lead(conn, int(lead_id))
        if not row:
            continue
        result = classify_buyer(dict(row), force_demo=force_demo)
        timestamp = now_iso()
        confidence = result.get("confidence")
        conn.execute(
            """
            UPDATE leads SET buyer_type = ?, classification_reason = ?,
            classification_method = ?, classification_confidence = ?,
            classification_at = ?, updated_at = ? WHERE id = ?
            """,
            (
                result["buyer_type"],
                result["reason"],
                result["method"],
                confidence,
                timestamp,
                timestamp,
                lead_id,
            ),
        )
        counts[result["buyer_type"]] = counts.get(result["buyer_type"], 0) + 1
        methods[result["method"]] = methods.get(result["method"], 0) + 1
    return {**counts, "methods": methods}


def record_email(
    conn: sqlite3.Connection,
    lead: sqlite3.Row | dict[str, Any],
    subject: str,
    body: str,
    attachment_name: str,
    status: str,
    provider: str,
    error: str = "",
) -> int:
    timestamp = now_iso()
    cursor = conn.execute(
        """
        INSERT INTO email_history (
            lead_id, lead_name, lead_email, subject, body, attachment_name,
            status, provider, error, sent_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            lead["id"], lead["contact_name"], lead["email"], subject, body,
            attachment_name, status, provider, error, timestamp,
        ),
    )
    conn.execute(
        "UPDATE leads SET last_contacted_at = ?, updated_at = ? WHERE id = ?",
        (timestamp, timestamp, lead["id"]),
    )
    return int(cursor.lastrowid)


def emails_to_lead_since(conn: sqlite3.Connection, lead_id: int, since_iso: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM email_history WHERE lead_id = ? AND sent_at >= ?",
        (lead_id, since_iso),
    ).fetchone()[0]


def get_email_history(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM email_history ORDER BY sent_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()


def record_import_batch(
    conn: sqlite3.Connection, filename: str, total: int, inserted: int, duplicates: int, invalid: int
) -> None:
    conn.execute(
        """
        INSERT INTO import_batches (filename, total_rows, inserted, duplicates, invalid, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (filename, total, inserted, duplicates, invalid, now_iso()),
    )


def record_discovery_run(
    conn: sqlite3.Connection,
    product: str, country: str, state: str, city: str,
    sources: list[str], found: int, inserted: int, duplicates: int,
) -> None:
    conn.execute(
        """
        INSERT INTO discovery_runs (product, country, state, city, sources, found, inserted, duplicates, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (product, country, state, city, ", ".join(sources), found, inserted, duplicates, now_iso()),
    )


def get_stats(conn: sqlite3.Connection) -> dict[str, int]:
    def scalar(sql: str, args: tuple[Any, ...] = ()) -> int:
        return int(conn.execute(sql, args).fetchone()[0])

    return {
        "total_leads": scalar("SELECT COUNT(*) FROM leads"),
        "valid_emails": scalar("SELECT COUNT(*) FROM leads WHERE email_valid = 1"),
        "invalid_emails": scalar("SELECT COUNT(*) FROM leads WHERE email_valid = 0"),
        "business_buyers": scalar("SELECT COUNT(*) FROM leads WHERE buyer_type = 'business'"),
        "individual_buyers": scalar("SELECT COUNT(*) FROM leads WHERE buyer_type = 'individual'"),
        "uncertain_buyers": scalar("SELECT COUNT(*) FROM leads WHERE buyer_type = 'uncertain'"),
        "unclassified": scalar("SELECT COUNT(*) FROM leads WHERE buyer_type = 'unclassified'"),
        "gemini_classified": scalar("SELECT COUNT(*) FROM leads WHERE classification_method LIKE 'Gemini AI%'"),
        "demo_classified": scalar("SELECT COUNT(*) FROM leads WHERE classification_method LIKE 'Demo rules%'"),
        "emails_logged": scalar("SELECT COUNT(*) FROM email_history"),
        "demo_leads": scalar("SELECT COUNT(*) FROM leads WHERE is_demo = 1"),
        "outreach_ready": scalar(
            "SELECT COUNT(*) FROM leads WHERE email_valid = 1 AND buyer_type = 'business'"
        ),
        "duplicates_skipped": scalar(
            "SELECT COALESCE(SUM(duplicates), 0) FROM import_batches"
        ) + scalar("SELECT COALESCE(SUM(duplicates), 0) FROM discovery_runs"),
    }


def get_group_counts(conn: sqlite3.Connection, column: str, limit: int = 10) -> list[tuple[str, int]]:
    allowed = {
        "source", "country", "state", "city", "buyer_type", "product",
        "status", "classification_method",
    }
    if column not in allowed:
        return []
    if column == "status":
        rows = conn.execute(
            "SELECT status AS label, COUNT(*) AS count FROM email_history GROUP BY status ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT COALESCE(NULLIF({column}, ''), 'Not provided') AS label, "
            f"COUNT(*) AS count FROM leads GROUP BY label ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [(row["label"], row["count"]) for row in rows]
