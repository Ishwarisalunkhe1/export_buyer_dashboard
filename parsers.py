"""CSV/XLSX lead-file parsing for the web upload workflow."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

MAX_ROWS = 5000

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "contact_name": ("contact name", "buyer name", "name", "contact", "person", "full name"),
    "company_name": ("company name", "company", "business", "showroom", "store name", "showroom name"),
    "email": ("email", "email address", "e-mail", "mail", "email id"),
    "phone": ("phone", "contact number", "contact no", "mobile", "telephone", "number"),
    "website": ("website", "web site", "site", "url", "company website"),
    "product": ("product", "product name", "category", "item"),
    "country": ("country",),
    "state": ("state", "region", "province"),
    "city": ("city", "town", "location"),
    "source": ("source", "lead source", "platform", "found via"),
    "source_url": ("source url", "link", "profile url", "listing url"),
    "role": ("role", "designation", "title", "job title", "position"),
}


def _normalize_header(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", " ").replace("-", " ")


def _map_row(row: dict[str, Any]) -> dict[str, str]:
    normalized = {_normalize_header(key): value for key, value in row.items()}
    mapped: dict[str, str] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapped[field] = str(normalized[alias] or "").strip()
                break
    return mapped


def _rows_to_leads(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    leads: list[dict[str, str]] = []
    for row in rows[:MAX_ROWS]:
        lead = _map_row(row)
        if any(lead.values()):
            leads.append(lead)
    return leads


def parse_lead_file(path: Path, filename: str) -> tuple[list[dict[str, str]], str]:
    suffix = Path(filename).suffix.lower()

    if suffix == ".csv":
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                with open(path, "r", encoding=encoding, newline="") as handle:
                    rows = list(csv.DictReader(handle))
                return _rows_to_leads(rows), ""
            except UnicodeDecodeError as exc:
                last_error = exc
        return [], f"Could not decode CSV: {type(last_error).__name__}"

    if suffix in {".xlsx", ".xlsm"}:
        try:
            from openpyxl import load_workbook
        except ImportError:
            return [], "openpyxl is required for Excel uploads (pip install openpyxl)."
        try:
            workbook = load_workbook(path, read_only=True, data_only=True)
            sheet = workbook.active
            values = list(sheet.iter_rows(values_only=True))
            workbook.close()
            if not values:
                return [], "The uploaded workbook is empty."
            headers = values[0]
            rows = [
                {str(headers[index] or f"column_{index}"): value for index, value in enumerate(row)}
                for row in values[1:]
            ]
            return _rows_to_leads(rows), ""
        except Exception as exc:
            return [], f"Could not read workbook: {type(exc).__name__}"

    if suffix == ".xls":
        return [], "Legacy .xls files are not supported. Please upload .csv or .xlsx."

    return [], "Unsupported file type. Upload a .csv or .xlsx lead file."
