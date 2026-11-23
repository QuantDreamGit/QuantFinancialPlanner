"""Verify PDF import, deterministic expense summaries, and source documents together."""

import base64
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.web.imports import handle_imports
from apps.web.statement_documents import handle_statement_documents
from apps.web.storage import Database
from apps.web.values import ApiError, euros


TODAY = date(2026, 11, 21)
PDF = b"%PDF-1.7\n% Synthetic statement\n%%EOF\n"
KEY = "sk-synthetic_OpenAI_import_test_key"


class OpenAIStatementImportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Database(Path(folder.name) / "ledger.sqlite3")
        self.database.initialize()
        self.account = self.database.save_account({
            "name": "Checking", "kind": "checking", "opened_on": "2026-09-01", "opening_balance": "1000",
        }, TODAY)
        transport = patch("apps.web.imports.extract_openai_statement")
        self.provider = transport.start()
        self.addCleanup(transport.stop)

    def request(self, resource, data=None, *, query=None):
        handler = SimpleNamespace(server=SimpleNamespace(database=self.database), database=self.database,
                                  read_data=lambda limit: data)
        method = "POST" if data is not None else "GET"
        result, status = handle_imports(handler, "/api/imports/" + resource, query or {}, method, TODAY)
        self.assertEqual(status, 201 if resource == "commit" else 200)
        return result

    def preview(self, pdf=PDF, **changes):
        return self.request("preview", {
            "source": "pdf", "pdf_method": "openai", "account_id": self.account, "filename": "Statement.pdf",
            "file": base64.b64encode(pdf).decode("ascii"), "api_key": KEY, "ai_consent": True, **changes,
        })

    def archive(self, document_id=None):
        handler = SimpleNamespace(database=self.database)
        path = "/api/imports/documents" + (f"/{document_id}" if document_id else "")
        return handle_statement_documents(handler, path, {}, "GET", TODAY)[0]

    @staticmethod
    def row(description, amount, reference, day="2026-11-16", page=1):
        return {"date": day, "description": description, "amount": amount, "currency": "EUR",
                "reference": reference, "page": page, "evidence": f"{description} {amount}", "issue": ""}

    @staticmethod
    def extraction(rows, debits, credits, *, note=""):
        return {"rows": rows, "notices": [], "document_type": "statement", "total_debit": debits,
                "total_credit": credits, "document_note": note}

    @staticmethod
    def selected(row, **fields):
        return {"token": row["token"], "line": row["line"], "name": row["name"], "posted_on": row["posted_on"],
                "amount": euros(int(row["amount"])), "action": "create", **fields}

    def test_imports_multiple_pdfs_with_exact_summary_and_durable_per_row_provenance(self):
        second_pdf = PDF + b"% another file"
        self.provider.side_effect = [
            self.extraction([
                self.row("Annual insurance policy", "-251.25", "BANK-1", page=3),
                self.row("Supermarket", "-250.00", "BANK-2"),
                self.row("Credit received", "+80.00", "BANK-3"),
            ], "501.25", "80.00"),
            self.extraction([self.row("Bakery", "-10.75", "BANK-4", day="2026-10-28")], "10.75", "0.00"),
        ]
        with patch("apps.web.imports._pdf_text", side_effect=AssertionError("unexpected local PDF handling")):
            first = self.preview()
            second = self.preview(second_pdf, filename="October.pdf")
        self.assertEqual(self.provider.call_args_list[0].args, (PDF,))
        self.assertEqual(self.provider.call_args_list[0].kwargs, {"api_key": KEY})
        self.assertEqual(first["name"], f"Bill - {TODAY.isoformat()}")
        for item, contents in ((first, PDF), (second, second_pdf)):
            self.assertEqual(item["extraction"]["totals_check"], "matched")
            self.assertTrue(all(row["review_group"] == "ready" for row in item["rows"]))
            self.assertEqual(self.archive(item["document"]["id"]).body, contents)
        self.assertEqual(first["summary"]["expense_total"], "50125")
        self.assertEqual(first["summary"]["small_total"], "25000")
        self.assertEqual(first["summary"]["credits_total"], "8000")
        self.assertEqual(self.database.ledger().entries, [])

        rows = [self.selected(row) for row in first["rows"] + second["rows"]]
        payload = {"name": first["name"], "rows": rows, "ready_only": True}
        posted = self.request("commit", payload)
        self.assertEqual(posted["saved"], 4)
        self.assertEqual(self.request("commit", payload)["already"], 4)
        batch = self.request("batch/" + str(posted["batch_id"]))
        report = batch["report"]
        self.assertEqual((report["expense_total"], report["small_total"], report["large_total"], report["credits_total"]),
                         ("51200", "26075", "25125", "8000"))
        self.assertEqual(report["small_groups"], [
            {"month": "2026-10", "amount": "1075", "count": 1},
            {"month": "2026-11", "amount": "25000", "count": 1},
        ])
        self.assertEqual(report["large_expenses"], [{
            "name": "Annual insurance policy", "posted_on": "2026-11-16", "amount": "25125",
            "reference": "BANK-1", "document_id": first["document"]["id"], "page": 3,
        }])
        self.assertEqual([document["filename"] for document in batch["documents"]], ["Statement.pdf", "October.pdf"])
        self.assertEqual(batch["rows"][0]["evidence"], "Annual insurance policy -251.25")
        self.assertEqual({entry["name"]: entry["amount"] for entry in self.database.ledger().entries},
                         {"Annual insurance policy": 25125, "Supermarket": 25000, "Credit received": 8000, "Bakery": 1075})

    def test_totals_mismatch_missing_totals_and_document_note_keep_rows_out_of_bulk_approval(self):
        row = self.row("Groceries", "-12.34", "BANK-1")
        for debits, credits, note, expected, hold in (
            ("22.34", "0.00", "", "mismatch", True),
            ("12.34", "", "", "partial", False),
            ("", "", "", "not_printed", False),
            ("12.34", "0.00", "The last page is cut off.", "matched", False),
        ):
            with self.subTest(debits=debits, credits=credits, note=note):
                self.provider.return_value = self.extraction([row], debits, credits, note=note)
                preview = self.preview()
                self.assertEqual(preview["extraction"]["totals_check"], expected)
                reviewed = preview["rows"][0]
                self.assertEqual(reviewed["review_group"], "review")
                self.assertEqual(reviewed.get("review_hold", False), hold)
                with self.assertRaises(ApiError) as declined:
                    self.request("commit", {"rows": [self.selected(reviewed)], "ready_only": True})
                self.assertEqual(declined.exception.status, 409)
        self.assertEqual(self.database.ledger().entries, [])
        self.assertEqual(self.archive()["total"], 1)

    def test_large_expense_keeps_the_original_description_after_a_shorter_ledger_name(self):
        description = "Annual insurance, contract for policy " + "😀" * 420
        self.provider.return_value = self.extraction([self.row(description, "-250.01", "BANK-2")], "250.01", "0")
        preview = self.preview()
        row = preview["rows"][0]
        self.assertEqual(row["statement_description"], description)
        batch = self.request("commit", {"rows": [self.selected(row, name="Insurance renewal")]})
        saved = self.request("batch/" + str(batch["batch_id"]))
        self.assertEqual(saved["rows"][0]["name"], "Insurance renewal")
        self.assertEqual(saved["report"]["large_expenses"][0]["name"], description)

    def test_consent_precedes_upload_and_provider_failure_leaves_one_downloadable_original(self):
        with self.assertRaises(ApiError) as denied:
            self.preview(ai_consent=False)
        self.assertEqual(denied.exception.field, "ai_consent")
        self.provider.assert_not_called()
        self.assertEqual(self.archive()["total"], 0)
        self.provider.side_effect = ApiError("Synthetic extraction failure", "file", 503)
        for _ in range(2):
            with self.assertRaises(ApiError):
                self.preview()
        archive = self.archive()
        self.assertEqual(archive["total"], 1)
        self.assertEqual(self.archive(archive["documents"][0]["id"]).body, PDF)
        self.assertEqual(self.database.ledger().entries, [])
