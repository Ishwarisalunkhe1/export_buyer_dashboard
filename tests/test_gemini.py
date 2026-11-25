"""Mocked Gemini adapter tests — no real API key or network call is used."""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import services  # noqa: E402


class FakeResponse:
    """Minimal urlopen context manager returning a fixed JSON body."""

    def __init__(self, body: str) -> None:
        self.buffer = io.BytesIO(body.encode("utf-8"))

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.buffer.close()

    def read(self) -> bytes:
        return self.buffer.getvalue()


def gemini_response(text: str) -> str:
    return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]})


class GeminiClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lead = {
            "contact_name": "Aarav Sharma",
            "company_name": "Lumen Decor LLC",
            "email": "hello@lumendecor.example.com",
            "website": "https://lumendecor.example.com",
            "role": "Import Manager",
            "country": "United States",
        }

    def test_gemini_success_returns_label_reason_confidence_and_source(self) -> None:
        seen: dict[str, object] = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["key"] = request.get_header("X-goog-api-key")
            seen["timeout"] = timeout
            return FakeResponse(gemini_response(
                '```json\n{"buyer_type": "Business buyer", "reason": "Imports home decor for retail showrooms.", "confidence": 1.4}\n```'
            ))

        with (
            patch.object(services, "GEMINI_API_KEY", "test-key-not-real"),
            patch.object(services, "GEMINI_MODEL", "gemini-test-model"),
            patch.object(services, "GEMINI_TIMEOUT_SECONDS", 7),
            patch.object(services.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            result = services.classify_buyer(self.lead)

        self.assertEqual(result["buyer_type"], "business")
        self.assertEqual(result["reason"], "Imports home decor for retail showrooms.")
        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(result["method"], "Gemini AI (gemini-test-model)")
        self.assertEqual(result["classification_error"], "")
        self.assertIn("gemini-test-model", str(seen["url"]))
        self.assertEqual(seen["key"], "test-key-not-real")
        self.assertEqual(seen["timeout"], 7)
        self.assertNotIn("test-key-not-real", str(result))

    def test_gemini_supports_business_individual_and_uncertain_labels(self) -> None:
        cases = {
            "business": "business",
            "individual buyer": "individual",
            "unclear": "uncertain",
        }
        with (
            patch.object(services, "GEMINI_API_KEY", "test-key-not-real"),
            patch.object(services, "GEMINI_MODEL", "gemini-test-model"),
            patch.object(services.urllib.request, "urlopen", return_value=FakeResponse(
                gemini_response('{"buyer_type": "business", "reason": "ok", "confidence": 0.5}')
            )),
        ):
            for raw_label, expected in cases.items():
                with self.subTest(raw_label=raw_label):
                    with patch.object(services.urllib.request, "urlopen", return_value=FakeResponse(
                        gemini_response(json.dumps({
                            "buyer_type": raw_label,
                            "reason": "Short reason.",
                            "confidence": 0,
                        }))
                    )):
                        result = services.classify_buyer(self.lead)
                    self.assertEqual(result["buyer_type"], expected)
                    self.assertEqual(result["confidence"], 0.0)
                    self.assertTrue(result["method"].startswith("Gemini AI ("))

    def test_invalid_gemini_response_falls_back_with_labelled_method(self) -> None:
        bodies = (
            "not json",
            '{"buyer_type": "alien", "reason": "unknown", "confidence": 0.9}',
            "[]",
        )
        with (
            patch.object(services, "GEMINI_API_KEY", "test-key-not-real"),
            patch.object(services, "GEMINI_MODEL", "gemini-test-model"),
        ):
            for body in bodies:
                with self.subTest(body=body):
                    with patch.object(services.urllib.request, "urlopen", return_value=FakeResponse(gemini_response(body))):
                        result = services.classify_buyer(self.lead)
                    self.assertEqual(result["classification_error"], "Gemini invalid response")
                    self.assertEqual(result["method"], "Demo rules (Gemini invalid response)")
                    self.assertTrue(result["reason"].startswith("Demo rules:"))
                    self.assertIsInstance(result["confidence"], float)

    def test_api_error_and_timeout_fall_back_without_raising(self) -> None:
        failures = (
            urllib.error.URLError("HTTP 503"),
            TimeoutError("timed out"),
        )
        with (
            patch.object(services, "GEMINI_API_KEY", "test-key-not-real"),
            patch.object(services, "GEMINI_MODEL", "gemini-test-model"),
        ):
            for error in failures:
                with self.subTest(error=type(error).__name__):
                    with patch.object(services.urllib.request, "urlopen", side_effect=error):
                        result = services.classify_buyer(self.lead)
                    self.assertIn("Demo rules (Gemini unavailable:", result["method"])
                    self.assertEqual(result["classification_error"], services._short_error(error))
                    self.assertNotIn("Gemini AI", result["method"])

    def test_missing_key_and_forced_demo_never_call_gemini(self) -> None:
        with (
            patch.object(services, "GEMINI_API_KEY", ""),
            patch.object(services.urllib.request, "urlopen", side_effect=AssertionError("unexpected call")) as urlopen,
        ):
            missing = services.classify_buyer(self.lead)
        urlopen.assert_not_called()
        self.assertEqual(missing["method"], "Demo rules (Gemini key not configured)")
        self.assertEqual(missing["classification_error"], "Gemini key not configured")

        with (
            patch.object(services, "GEMINI_API_KEY", "test-key-not-real"),
            patch.object(services.urllib.request, "urlopen", side_effect=AssertionError("unexpected call")) as urlopen,
        ):
            forced = services.classify_buyer(self.lead, force_demo=True)
        urlopen.assert_not_called()
        self.assertEqual(forced["method"], "Demo rules (forced demo)")


if __name__ == "__main__":
    unittest.main()
