"""Check explicit AI consent, signed amounts, and normal import accounting."""

import base64
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.web.__main__ import DashboardHandler
from apps.web.import_ai import extract_statement
from apps.web.storage import Database
from apps.web.values import ApiError, euros


TODAY = date(2026, 11, 20)
PDF_DATA = b"%PDF-1.4 synthetic placeholder"
PDF_TEXT = "2026-11-18 Synthetic bank debit -12.34"
KEY = "gsk_synthetic_test_credential_that_must_not_be_returned"


class PdfAiImportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "test.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()
        self.account = self.database.save_account({
            "name": "Checking", "kind": "checking",
            "opened_on": "2026-10-01", "opening_balance": "500",
        }, TODAY)
        self.server = SimpleNamespace(database=self.database, projection=None)
        mocks = ExitStack()
        self.addCleanup(mocks.close)
        server_date = mocks.enter_context(patch("apps.web.__main__.date"))
        server_date.today.return_value = TODAY
        mocks.enter_context(patch("apps.web.imports._pdf_text", return_value=PDF_TEXT))
        self.extract = mocks.enter_context(patch("apps.web.imports.extract_statement"))
        self.extract.return_value = {"rows": [self.ai_row()], "notices": []}
        mocks.enter_context(patch.dict(os.environ))
        os.environ.pop("GROQ_API_KEY", None)

    @staticmethod
    def ai_row(**changes):
        return {
            "date": "2026-11-18", "description": "Synthetic bank debit", "amount": "-12.34",
            "currency": "EUR", "reference": "", **changes,
        }

    def api(self, method, resource, data=None, *, status=200):
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.command = method
        handler.server = self.server
        handler.headers = Message()
        if data is not None:
            body = json.dumps(data).encode()
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = str(len(body))
            handler.rfile = BytesIO(body)
        result, actual_status = handler.api("/api/imports/" + resource, {})
        self.assertEqual(actual_status, status, result)
        return result

    def request(self, **changes):
        return {
            "source": "pdf", "account_id": self.account, "file": base64.b64encode(PDF_DATA).decode(),
            "pdf_method": "ai", "ai_consent": True, "api_key": KEY, **changes,
        }

    @staticmethod
    def selected(row, action="create", **changes):
        return {
            "token": row["token"], "line": row["line"], "posted_on": row["posted_on"], "name": row["name"],
            "amount": euros(int(row["amount"])), "action": action, **changes,
        }

    def test_setup_reports_provider_availability_without_returning_key(self):
        no_key = self.api("GET", "setup")
        self.assertEqual(no_key["ai"]["provider"], "Groq")
        self.assertFalse(no_key["ai"]["configured"])
        with patch.dict(os.environ, {"GROQ_API_KEY": KEY}):
            configured = self.api("GET", "setup")
        self.assertTrue(configured["ai"]["configured"])
        self.assertNotIn(KEY, json.dumps(configured))
        self.extract.assert_not_called()

    def test_local_pdf_preview_and_column_inspection_never_send_text(self):
        local = self.request()
        del local["pdf_method"]
        default = self.api("POST", "preview", local)
        self.assertEqual(default["rows"][0]["amount"], "-1234")
        # Column inspection may reject the request or show only locally parsed rows.
        try:
            self.api("POST", "inspect", self.request())
        except ApiError as error:
            self.assertLess(error.status, 500)
        self.assertEqual(self.database.ledger().entries, [])
        self.extract.assert_not_called()

    def test_provider_requires_pdf_preview_explicit_boolean_consent_and_valid_account(self):
        for consent in (None, False, "true", 1):
            with self.subTest(consent=consent), self.assertRaises(ApiError) as declined:
                self.api("POST", "preview", self.request(ai_consent=consent))
            self.assertLess(declined.exception.status, 500)
        csv = base64.b64encode(b"Date,Description,Amount\n2026-11-18,Shop,-12.34\n").decode()
        with self.assertRaises(ApiError):
            self.api("POST", "preview", self.request(source="csv", file=csv,
                                                      mapping={"date": 0, "name": 1, "amount": 2}))
        with self.assertRaises(ApiError):
            self.api("POST", "preview", self.request(account_id=999))
        self.extract.assert_not_called()
        self.assertEqual(self.api("GET", "setup")["history"], [])

    def test_missing_optional_server_and_request_keys_never_contact_provider(self):
        request = self.request()
        del request["api_key"]
        with patch("apps.web.imports.extract_statement", extract_statement), patch("apps.web.import_ai._request") as transport:
            with self.assertRaises(ApiError) as error:
                self.api("POST", "preview", request)
            self.assertEqual(error.exception.field, "api_key")
            transport.assert_not_called()

    def test_review_token_is_bound_to_the_ledger_that_was_previewed(self):
        preview = self.api("POST", "preview", self.request())
        row = self.selected(preview["rows"][0])
        other = Database(self.path.with_name("other.sqlite3"))
        other.initialize()
        account = other.save_account({
            "name": "Other ledger", "kind": "checking",
            "opened_on": "2026-10-01", "opening_balance": "100",
        }, TODAY)
        self.assertEqual(account, self.account)
        self.server.database = other
        payload = {"name": preview["name"], "rows": [row]}
        with self.assertRaises(ApiError):
            self.api("POST", "commit", payload, status=201)
        self.assertEqual(other.ledger().entries, [])
        self.server.database = self.database
        self.assertEqual(self.api("POST", "commit", payload, status=201)["saved"], 1)

    def test_ai_preview_passes_local_text_and_reuses_receipt_and_duplicate_pipeline(self):
        entry_id = self.database.save_entry({
            "name": "Existing bank debit", "kind": "expense", "account_id": self.account,
            "posted_on": "2026-11-18", "amount": "12.34",
        }, TODAY)
        self.extract.return_value = {"rows": [self.ai_row(reference="BANK-001")], "notices": []}
        preview = self.api("POST", "preview", self.request(name="November statement"))
        self.extract.assert_called_once_with(PDF_TEXT, api_key=KEY)
        self.assertEqual((preview["pdf_method"], preview["name"]), ("ai", "November statement"))
        self.assertNotIn(KEY, json.dumps(preview))
        self.assertEqual(len(self.database.ledger().entries), 1)
        row = preview["rows"][0]
        self.assertEqual(row["review_group"], "matches")
        self.assertEqual(row["matches"][0]["id"], entry_id)
        payload = {"name": preview["name"], "rows": [self.selected(row, "link", entry_id=entry_id)]}
        posted = self.api("POST", "commit", payload, status=201)
        repeated = self.api("POST", "commit", payload, status=201)
        self.assertEqual((posted["saved"], repeated["already"]), (1, 1))
        self.assertEqual(len(self.database.ledger().entries), 1)
        history = self.api("GET", "setup")["history"]
        batch = self.api("GET", "batch/" + str(posted["batch_id"]))
        self.assertEqual((history[0]["name"], batch["batch"]["name"]), ("November statement", "November statement"))
        self.assertEqual((batch["rows"][0]["reference"], batch["rows"][0]["action"]), ("BANK-001", "link"))
        self.assertNotIn(KEY, json.dumps({"history": history, "batch": batch, "posted": posted}))
        with self.database.connection(snapshot=True) as connection:
            self.assertNotIn(KEY, "\n".join(connection.iterdump()))

    def test_ai_credit_is_income_even_with_reversed_manual_amount_convention(self):
        self.extract.return_value = {"rows": [
            self.ai_row(description="", amount="+5.25"),
        ], "notices": []}
        preview = self.api("POST", "preview", self.request(amount_mode="debit-positive"))
        row = preview["rows"][0]
        self.assertEqual(preview["name"], "Bill - 2026-11-20")
        self.assertEqual((row["name"], row["amount"]), ("Bill - 2026-11-20", "525"))
        response = self.api("POST", "commit", {"name": preview["name"], "rows": [self.selected(row)]}, status=201)
        self.assertEqual(response["saved"], 1)
        entry = self.database.ledger().entries[0]
        self.assertEqual((entry["kind"], entry["amount"]), ("income", 525))
        self.assertEqual(self.api("GET", "setup")["history"][0]["name"], preview["name"])
        again = self.api("POST", "preview", self.request(name="Renamed bill"))
        self.assertEqual(again["rows"][0]["name"], "Renamed bill")
        self.assertEqual(again["rows"][0]["review_group"], "handled")

    def test_foreign_currency_and_pdf_bulk_approval_guards_hold_for_ai_rows(self):
        self.extract.return_value = {"rows": [
            self.ai_row(description="Currency mismatch", currency="USD"),
            self.ai_row(description="Review first", amount="-9.87"),
        ], "notices": []}
        preview = self.api("POST", "preview", self.request())
        foreign, review = preview["rows"]
        self.assertTrue(foreign["blocked"])
        self.assertEqual(review["review_group"], "review")
        with self.assertRaises(ApiError):
            self.api("POST", "commit", {"name": preview["name"], "rows": [self.selected(foreign)]}, status=201)
        with self.assertRaises(ApiError):
            self.api("POST", "commit", {"name": preview["name"], "rows": [self.selected(review)],
                                       "ready_only": True}, status=201)
        self.assertEqual(self.database.ledger().entries, [])
        self.assertEqual(self.api("GET", "setup")["history"], [])

    def test_unknown_extracted_currency_requires_affirmative_euro_confirmation(self):
        self.extract.return_value = {"rows": [self.ai_row(currency="")], "notices": []}
        preview = self.api("POST", "preview", self.request())
        row = preview["rows"][0]
        self.assertEqual(row["currency"], "")
        self.assertTrue(row["currency_unconfirmed"])
        self.assertFalse(row["blocked"])
        for consent in (None, False, "true"):
            with self.subTest(consent=consent), self.assertRaises(ApiError):
                self.api("POST", "commit", {
                    "name": preview["name"], "rows": [self.selected(row, currency_confirmed=consent)],
                }, status=201)
        self.assertEqual(self.database.ledger().entries, [])
        posted = self.api("POST", "commit", {
            "name": preview["name"], "rows": [self.selected(row, currency_confirmed=True)],
        }, status=201)
        self.assertEqual(posted["saved"], 1)
