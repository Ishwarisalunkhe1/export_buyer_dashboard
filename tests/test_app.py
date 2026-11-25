"""End-to-end tests for the buyer discovery dashboard."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DIR = Path(tempfile.mkdtemp(prefix="export-dashboard-test-"))
os.environ["EXPORT_DASHBOARD_DB"] = str(TEST_DIR / "test.db")
os.environ["EXPORT_DASHBOARD_SEED"] = "1"
os.environ["EMAIL_VALIDATION_MODE"] = "format"
os.environ["GEMINI_API_KEY"] = ""
os.environ["GMAIL_ACCESS_TOKEN"] = ""

from app import create_app  # noqa: E402
from config import SAMPLE_PDF_PATH  # noqa: E402
from database import connect  # noqa: E402
import services  # noqa: E402


class DashboardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        db = connect()
        db.executescript(
            """
            DROP TABLE IF EXISTS leads;
            DROP TABLE IF EXISTS email_history;
            DROP TABLE IF EXISTS import_batches;
            DROP TABLE IF EXISTS discovery_runs;
            """
        )
        db.close()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def lead_by_email(self, email: str):
        db = connect()
        row = db.execute("SELECT * FROM leads WHERE email = ?", (email,)).fetchone()
        db.close()
        return row

    def count(self, table: str) -> int:
        allowed = {"leads", "email_history", "import_batches", "discovery_runs"}
        assert table in allowed
        db = connect()
        value = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        db.close()
        return value

    def test_health_and_main_pages(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)
        for path in ("/", "/discovery", "/leads", "/outreach", "/history", "/reports", "/playbooks", "/integrations"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_add_lead_and_duplicate_prevention(self) -> None:
        response = self.client.post(
            "/leads/add",
            data={
                "contact_name": "Test Buyer", "company_name": "Test Decor Store",
                "email": "buyer@testdecor.example.com", "phone": "+1 555 0100",
                "website": "https://testdecor.example.com", "product": "singing bowls",
                "country": "United States", "state": "Texas", "city": "Houston",
                "source": "Google Maps", "role": "Buyer",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Added lead", response.data)
        self.assertEqual(self.count("leads"), 11)

        duplicate = self.client.post(
            "/leads/add",
            data={
                "contact_name": "Another Name", "company_name": "Different Store",
                "email": "BUYER@TestDecor.example.com", "website": "https://other.example.com",
                "country": "United States", "state": "Texas", "city": "Houston",
                "source": "LinkedIn",
            },
            follow_redirects=True,
        )
        self.assertIn(b"duplicate email address", duplicate.data)
        self.assertEqual(self.count("leads"), 11)

    def test_csv_import_maps_columns_and_skips_duplicates(self) -> None:
        csv_data = (
            "Buyer Name,Company,Email,Contact Number,Website,Country,State,City,Source\n"
            "New Buyer,New Decor Imports,new@example.com,+1 555 0199,https://newdecor.example.com,"
            "United States,Texas,Houston,ImportYeti\n"
            "Duplicate Buyer,Lumen Decor LLC,hello@lumendecor.example.com,+1 713 555 0101,"
            "https://lumendecor.example.com,United States,Texas,Houston,Google Maps\n"
            ",,,,,,United States,Texas,Houston,Yelp\n"
        )
        response = self.client.post(
            "/leads/import",
            data={
                "lead_file": (io.BytesIO(csv_data.encode("utf-8")), "leads.csv"),
                "product": "singing bowls", "source": "File upload",
                "country": "United States", "state": "Texas",
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"1 added", response.data)
        self.assertIn(b"1 duplicate(s) skipped", response.data)
        self.assertIn(b"1 row(s) without usable contact details", response.data)
        self.assertEqual(self.count("leads"), 11)
        self.assertEqual(self.count("import_batches"), 1)

    def test_validation_and_business_classification(self) -> None:
        invalid = self.lead_by_email("jonathan[at]harmonyhome.example.com")
        business = self.lead_by_email("hello@lumendecor.example.com")
        individual = self.lead_by_email("emily.carter@example.com")
        self.assertIsNotNone(invalid)
        self.assertIsNotNone(business)
        self.assertIsNotNone(individual)

        validation_response = self.client.post(
            "/leads/bulk",
            data={"action": "validate", "lead_ids": [str(invalid["id"]), str(business["id"])]},
            follow_redirects=True,
        )
        self.assertIn(b"1 valid, 1 invalid", validation_response.data)

        classify_response = self.client.post(
            "/leads/bulk",
            data={"action": "classify", "lead_ids": [str(business["id"]), str(individual["id"])]},
            follow_redirects=True,
        )
        self.assertIn(b"Classification complete", classify_response.data)

        db = connect()
        self.assertEqual(
            db.execute("SELECT email_valid FROM leads WHERE id = ?", (invalid["id"],)).fetchone()[0], 0
        )
        self.assertEqual(
            db.execute("SELECT buyer_type FROM leads WHERE id = ?", (business["id"],)).fetchone()[0], "business"
        )
        self.assertEqual(
            db.execute("SELECT buyer_type FROM leads WHERE id = ?", (individual["id"],)).fetchone()[0], "individual"
        )
        db.close()

    def test_discovery_links_and_demo_run(self) -> None:
        page = self.client.get("/discovery?product=singing+bowls&country=United+States&state=Texas&city=Houston")
        self.assertEqual(page.status_code, 200)
        for text in (b"Google Maps", b"LinkedIn People", b"ImportYeti", b"Houston, Texas, United States"):
            self.assertIn(text, page.data)

        run = self.client.post(
            "/discovery/run",
            data={
                "product": "singing bowls", "country": "United States",
                "state": "Texas", "city": "Houston",
                "sources": ["google_maps", "importyeti", "yelp"],
            },
            follow_redirects=True,
        )
        self.assertIn(b"2 new lead(s), 1 duplicate(s) skipped", run.data)
        self.assertEqual(self.count("leads"), 12)
        self.assertEqual(self.count("discovery_runs"), 1)

    def test_demo_outreach_personalization_attachment_and_spam_guard(self) -> None:
        lead = self.lead_by_email("hello@lumendecor.example.com")
        with connect() as db:
            db.execute(
                "UPDATE leads SET email_valid = 1, buyer_type = 'business' WHERE id = ?", (lead["id"],)
            )

        send = self.client.post(
            "/outreach/send",
            data={
                "lead_ids": str(lead["id"]),
                "subject": "Supplier of {product} for {company}",
                "body": "Hello {first_name}, please see our {product} catalogue for {city}.",
                "attachment": SAMPLE_PDF_PATH.name,
                "sender_name": "Export Team", "company_name": "India Exports",
                "product": "singing bowls", "country": "United States",
                "business_only": "1", "max_emails": "5",
            },
            follow_redirects=True,
        )
        self.assertIn(b"1 sent/simulated", send.data)
        self.assertEqual(self.count("email_history"), 1)

        db = connect()
        history = db.execute("SELECT * FROM email_history").fetchone()
        db.close()
        self.assertEqual(history["status"], "demo_sent")
        self.assertEqual(history["provider"], "demo-outbox")
        self.assertEqual(history["attachment_name"], SAMPLE_PDF_PATH.name)
        self.assertIn(b"Lumen Decor LLC", history["subject"].encode())
        self.assertIn(b"Aarav", history["body"].encode())
        self.assertIn(b"singing bowls", history["body"].encode())
        self.assertIn(b"Demo only", history["error"].encode())

        repeat = self.client.post(
            "/outreach/send",
            data={
                "lead_ids": str(lead["id"]), "subject": "Follow up", "body": "Hello again",
                "business_only": "1", "max_emails": "5",
            },
            follow_redirects=True,
        )
        self.assertIn(b"contacted in the last 24 hours", repeat.data)
        self.assertEqual(self.count("email_history"), 1)

    def test_reports_and_csv_export(self) -> None:
        response = self.client.get("/reports")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Data quality summary", response.data)
        self.assertIn(b"Classification sources", response.data)

        csv_response = self.client.get("/reports/leads.csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("text/csv", csv_response.content_type)
        self.assertIn(b"contact_name,company_name,email", csv_response.data)

    def test_edit_view_delete_lead_and_duplicate_website(self) -> None:
        added = self.client.post(
            "/leads/add",
            data={
                "contact_name": "Workflow Buyer", "company_name": "Workflow Site Store",
                "email": "workflow@workflowsite.example.com", "phone": "+1 555 0101",
                "website": "https://workflowsite.example.com", "product": "singing bowls",
                "country": "United States", "state": "Texas", "city": "Houston",
                "source": "Manual entry", "role": "Buyer",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Added lead", added.data)
        lead = self.lead_by_email("workflow@workflowsite.example.com")
        self.assertIsNotNone(lead)

        detail = self.client.get(f"/leads/{lead['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Workflow Buyer", detail.data)
        self.assertIn(b"Real entry", detail.data)

        edited = self.client.post(
            f"/leads/{lead['id']}/edit",
            data={
                "contact_name": "Workflow Buyer Updated", "company_name": "Workflow Site Store",
                "email": "workflow@workflowsite.example.com", "phone": "+1 555 0222",
                "website": "https://workflowsite.example.com", "product": "singing bowls",
                "country": "United States", "state": "Texas", "city": "Houston",
                "source": "Manual entry", "role": "Purchaser",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Lead updated", edited.data)
        with connect() as db:
            updated = db.execute("SELECT * FROM leads WHERE id = ?", (lead["id"],)).fetchone()
        self.assertEqual(updated["contact_name"], "Workflow Buyer Updated")
        self.assertEqual(updated["role"], "Purchaser")

        duplicate_website = self.client.post(
            "/leads/add",
            data={
                "contact_name": "Website Duplicate", "company_name": "Other Site Store",
                "email": "other@workflowsite.example.com",
                "website": "https://www.workflowsite.example.com/contact",
                "country": "United States", "state": "Texas", "city": "Austin",
                "source": "LinkedIn",
            },
            follow_redirects=True,
        )
        self.assertIn(b"duplicate website", duplicate_website.data)
        self.assertEqual(self.count("leads"), 11)

        deleted = self.client.post(f"/leads/{lead['id']}/delete", follow_redirects=True)
        self.assertIn(b"Lead deleted", deleted.data)
        self.assertIsNone(self.lead_by_email("workflow@workflowsite.example.com"))
        self.assertEqual(self.count("leads"), 10)

    def test_demo_leads_remain_labelled(self) -> None:
        with connect() as db:
            demo_count = db.execute("SELECT COUNT(*) FROM leads WHERE is_demo = 1").fetchone()[0]
        self.assertEqual(demo_count, 10)
        page = self.client.get("/leads")
        self.assertIn(b">Demo</span>", page.data)

    def test_invalid_email_is_excluded_from_outreach(self) -> None:
        invalid = self.lead_by_email("jonathan[at]harmonyhome.example.com")
        with connect() as db:
            db.execute(
                "UPDATE leads SET buyer_type = 'business', email_valid = 0 WHERE id = ?",
                (invalid["id"],),
            )

        outreach_page = self.client.get("/outreach")
        self.assertNotIn(b"jonathan[at]harmonyhome.example.com", outreach_page.data)

        send = self.client.post(
            "/outreach/send",
            data={
                "lead_ids": str(invalid["id"]), "subject": "Hello", "body": "Hello there",
                "business_only": "1", "max_emails": "5",
            },
            follow_redirects=True,
        )
        self.assertIn(b"invalid email address", send.data)
        self.assertEqual(self.count("email_history"), 0)

    def test_demo_classification_persists_after_new_app_instance(self) -> None:
        lead = self.lead_by_email("hello@lumendecor.example.com")
        response = self.client.post(
            "/leads/bulk",
            data={"action": "classify", "lead_ids": [str(lead["id"])], "force_demo": "1"},
            follow_redirects=True,
        )
        self.assertIn(b"Demo rules (forced demo)", response.data)

        fields = (
            "buyer_type", "classification_reason", "classification_method",
            "classification_confidence", "classification_at",
        )
        with connect() as db:
            before = dict(db.execute(
                f"SELECT {', '.join(fields)} FROM leads WHERE id = ?", (lead["id"],)
            ).fetchone())
        self.assertTrue(before["classification_method"].startswith("Demo rules"))
        self.assertIsInstance(before["classification_confidence"], float)
        self.assertTrue(before["classification_at"])

        create_app()  # Simulate a Flask restart without dropping SQLite data.
        with connect() as db:
            after = dict(db.execute(
                f"SELECT {', '.join(fields)} FROM leads WHERE id = ?", (lead["id"],)
            ).fetchone())
        self.assertEqual(after, before)

    def test_gemini_route_persists_source_and_does_not_expose_key(self) -> None:
        lead = self.lead_by_email("hello@lumendecor.example.com")
        body = json.dumps({
            "candidates": [{
                "content": {"parts": [{
                    "text": json.dumps({
                        "buyer_type": "business",
                        "reason": "Verified importer of home decor.",
                        "confidence": 0.83,
                    })
                }]}
            }]
        }).encode("utf-8")

        with (
            patch.object(services, "GEMINI_API_KEY", "test-key-not-real"),
            patch.object(services, "GEMINI_MODEL", "gemini-test-model"),
            patch.object(services.urllib.request, "urlopen", return_value=io.BytesIO(body)),
        ):
            response = self.client.post(
                f"/leads/{lead['id']}/classify",
                data={"force_demo": "0"},
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Gemini AI (gemini-test-model)", response.data)
        self.assertIn(b"83%", response.data)
        self.assertNotIn(b"test-key-not-real", response.data)

        with connect() as db:
            row = db.execute(
                "SELECT buyer_type, classification_method, classification_reason, "
                "classification_confidence, classification_at FROM leads WHERE id = ?",
                (lead["id"],),
            ).fetchone()
        self.assertEqual(row["buyer_type"], "business")
        self.assertEqual(row["classification_method"], "Gemini AI (gemini-test-model)")
        self.assertEqual(row["classification_reason"], "Verified importer of home decor.")
        self.assertEqual(row["classification_confidence"], 0.83)
        self.assertTrue(row["classification_at"])


if __name__ == "__main__":
    unittest.main()
