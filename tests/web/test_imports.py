import base64
import io
import json
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.web.attachments import download_attachment, save_attachment
from apps.web.attachments import list_attachments, save_attachment
from apps.web.forecast import save_rule
from apps.web.imports import _pdf_backend, _pdf_rows, handle_imports, initialize_imports, parse_amount
from apps.web.reports import dashboard
from apps.web.shared import handle_shared
from apps.web.storage import Database
from apps.web.values import ApiError, euros


TODAY = date(2026, 11, 20)


def sample_pdf(content):
    stream = b"BT /F1 11 Tf 36 740 Td (" + content + b") Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(document)
    document.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        document.extend(f"{offset:010} 00000 n \n".encode())
    document.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(document)


class ImportsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(Path(folder.name) / "test.sqlite3")
        self.db.initialize()
        initialize_imports(self.db)
        self.checking = self.account("Checking", "checking")
        self.savings = self.account("Savings", "savings")

    def account(self, name, kind):
        return self.db.save_account({"name": name, "kind": kind, "opening_balance": "500",
                                     "opened_on": "2026-01-01"}, TODAY)

    def api(self, route, data=None, *, shared=False):
        method = "GET" if data is None else "POST"
        handler = SimpleNamespace(server=SimpleNamespace(database=self.db), read_data=lambda _: data)
        runner = handle_shared if shared else handle_imports
        payload, status = runner(handler, ("/api/shared" if shared else "/api/imports") + route, {}, method, TODAY)
        self.assertIn(status, (200, 201))
        return payload

    def input(self, value, source="csv", **overrides):
        return {
            "file": base64.b64encode(value.encode() if isinstance(value, str) else value).decode(),
            "source": source, "account_id": self.checking, **overrides,
        }

    def preview(self, data):
        if data["source"] == "csv" and "mapping" not in data:
            data = {**data, "mapping": self.api("/inspect", data)["mapping"]}
        return self.api("/preview", data)

    @staticmethod
    def decide(row, action="create", **overrides):
        amount = euros(int(row["amount"])) if row["amount"] is not None else ""
        return {
            "token": row["token"], "line": row["line"], "action": action,
            "name": row["name"], "posted_on": row["posted_on"], "amount": amount, **overrides,
        }

    def commit(self, *decisions, ready_only=False):
        return self.api("/commit", {"rows": list(decisions), "ready_only": ready_only})

    def test_review_groups_keep_unverified_rows_out_of_bulk_approval(self):
        existing = self.db.save_entry({"kind": "expense", "name": "Shop", "amount": "14.50",
                                       "account_id": self.checking, "posted_on": "2026-11-15"}, TODAY)
        data = self.input("Date,Description,Amount\n2026-11-15,City Market,-14.50\n2026-11-16,Fare,-1.25\n"
                          "2026-11-17,Unclear,bad\n2026-11-18,Lunch,-2.75\n2026-11-18,Lunch,-2.75\n")
        rows = self.preview(data)["rows"]
        self.assertEqual([row["review_group"] for row in rows], ["matches", "ready", "review", "review", "review"])
        self.assertEqual((rows[0]["matches"][0]["id"], rows[0]["matches"][0]["amount"]), (existing, "-1450"))
        self.assertIsNone(rows[0]["auto_match"])
        with self.assertRaisesRegex(ApiError, "no longer ready"):
            self.commit(self.decide(rows[1]), self.decide(rows[3]), ready_only=True)
        self.assertEqual(len(self.db.ledger().entries), 1)
        bulk = self.commit(self.decide(rows[1]), ready_only=True)
        self.assertEqual(bulk["saved"], 1)
        self.assertEqual(self.commit(self.decide(rows[1]), ready_only=True)["already"], 1)
        self.assertEqual(self.preview(data)["rows"][1]["review_group"], "handled")

    def test_openai_csv_maps_samples_and_parses_every_transaction_locally(self):
        columns = "Contabile;Causale;Addebiti;Accrediti;Saldo;Valuta;Codice"
        purchases = [f"16/11/2026;Merchant {index};1.234,56;;5.000,00;EUR;BANK-{index}" for index in range(12)]
        credit = "18/11/2026;Refund;;24,50;5.024,50;EUR;BANK-12"
        data = self.input("\n".join([columns, *purchases, credit]), csv_method="openai",
                          api_key="sk-synthetic-csv-test-key", openai_model="gpt-5.4-mini")
        layout = {"mapping": {"date": 0, "name": 1, "debit": 2, "credit": 3, "currency": 5, "reference": 6},
                  "date_format": "dmy", "amount_mode": "separate"}
        with patch("apps.web.imports.suggest_csv_mapping", return_value=layout) as suggest:
            with self.assertRaisesRegex(ApiError, "Confirm that Quant may send"):
                self.api("/preview", data)
            with self.assertRaises(ApiError) as invalid_model:
                self.api("/preview", dict(data, ai_consent=True, openai_model="unknown"))
            self.assertEqual(invalid_model.exception.field, "openai_model")
            suggest.assert_not_called()
            preview = self.api("/preview", dict(data, ai_consent=True))
        headers, samples = suggest.call_args.args
        self.assertEqual(headers, columns.split(";"))
        self.assertEqual(len(samples), 8)
        self.assertEqual(samples[0][1], "Merchant 0")
        self.assertEqual(samples[-1][1], "Refund")
        self.assertEqual(suggest.call_args.kwargs, {"api_key": data["api_key"], "model": data["openai_model"]})
        self.assertEqual(preview["openai_model"], data["openai_model"])
        self.assertEqual(preview["csv_layout"]["mapping"], layout["mapping"])
        self.assertEqual(len(preview["rows"]), 13)
        rows = preview["rows"]
        self.assertEqual((rows[4]["posted_on"], rows[4]["name"], rows[4]["amount"]), ("2026-11-16", "Merchant 4", "-123456"))
        self.assertEqual((rows[-1]["name"], rows[-1]["amount"]), ("Refund", "2450"))
        manual = self.api("/preview", dict(data, csv_method="local", **layout))
        self.assertEqual([row["review_key"] for row in rows], [row["review_key"] for row in manual["rows"]])

    def test_openai_csv_recovers_missing_columns_and_a_single_debit_column(self):
        data = self.input("Data valuta;Data contabile;Descrizione;Addebiti;Saldo\n"
                          "06/11/2026;05/11/2026;Market;8,75;100,00\n"
                          "20/11/2026;19/11/2026;Train;24,50;75,50\n",
                          csv_method="openai", ai_consent=True, api_key="sk-synthetic-csv-test-key")
        incomplete = {"mapping": {"debit": 3}, "date_format": "auto", "amount_mode": "separate"}
        with patch("apps.web.imports.suggest_csv_mapping", return_value=incomplete):
            preview = self.api("/preview", data)
        layout = preview["csv_layout"]
        self.assertEqual(layout["mapping"], {"date": 1, "name": 2, "debit": 3})
        self.assertEqual(layout["date_format"], "dmy")
        self.assertEqual([(row["posted_on"], row["name"], row["amount"]) for row in preview["rows"]],
                         [("2026-11-05", "Market", "-875"), ("2026-11-19", "Train", "-2450")])
        manual = self.api("/preview", dict(data, csv_method="local", mapping=layout["mapping"],
                                          date_format=layout["date_format"], amount_mode=layout["amount_mode"]))
        self.assertEqual([row["review_key"] for row in manual["rows"]], [row["review_key"] for row in preview["rows"]])

    def test_openai_csv_keeps_unmapped_rows_editable_without_guessing_values(self):
        data = self.input("Memo;Amount;Tag\nMetro;4,80;Commute\nLunch;9,90;Work\n",
                          csv_method="openai", ai_consent=True, api_key="sk-synthetic-csv-test-key")
        incomplete = {"mapping": {"name": 0, "amount": 1}, "date_format": "auto", "amount_mode": None}
        with patch("apps.web.imports.suggest_csv_mapping", return_value=incomplete):
            preview = self.api("/preview", data)
        rows = preview["rows"]
        self.assertEqual([(row["posted_on"], row["name"], row["amount"]) for row in rows],
                         [("", "Metro", None), ("", "Lunch", None)])
        self.assertIn("posting date", preview["notices"][0])
        self.assertIn("amount signs", preview["notices"][1])
        self.assertIn("Amount: 4,80", rows[0]["csv_source"])
        with self.assertRaises(ApiError):
            self.commit(self.decide(rows[0], posted_on="2026-11-16"))
        self.assertEqual(self.commit(self.decide(rows[0], posted_on="2026-11-16", amount="-4.80"))["saved"], 1)

    def test_backdated_import_extends_history_only_on_successful_commit(self):
        account = self.account("New checking", "checking")
        self.db.save_account({"name": "New checking", "opening_balance": "500", "opened_on": TODAY.isoformat()}, TODAY, account)
        rows = self.preview(self.input("Date,Description,Amount\n2026-11-16,Fare,-8.50\n2026-11-18,Refund,4.25\n",
                                       account_id=account))["rows"]

        def account_snapshot():
            return next(item for item in self.db.ledger().accounts if item["id"] == account)

        self.assertEqual([row["errors"] for row in rows], [[], []])
        self.assertEqual(account_snapshot()["opened_on"], TODAY.isoformat())
        with self.assertRaises(ApiError):
            self.commit(self.decide(rows[0]), self.decide(rows[1], posted_on="2026-11-21"))
        self.assertEqual(account_snapshot()["opened_on"], TODAY.isoformat())
        self.assertEqual(self.db.ledger().entries, [])

        edits = (self.decide(rows[0], verify_ready=True), self.decide(rows[1], posted_on="2026-11-12"))
        self.assertEqual(self.commit(*edits)["saved"], 2)
        saved = account_snapshot()
        self.assertEqual((saved["opened_on"], saved["opening_balance"]), ("2026-11-12", 50_000))
        report = dashboard(self.db.ledger(), "ALL", TODAY)
        self.assertEqual(next(item["balance"] for item in report["accounts"] if item["id"] == account), "49575")
        self.assertEqual(self.commit(*edits)["already"], 2)
        self.assertEqual(account_snapshot(), saved)

    def test_backdated_transfer_extends_both_accounts(self):
        for account, name in ((self.checking, "Checking"), (self.savings, "Savings")):
            self.db.save_account({"name": name, "opening_balance": "500", "opened_on": "2026-11-19"}, TODAY, account)
        row = self.preview(self.input("Date,Description,Amount\n2026-11-16,Move to savings,-50\n"))["rows"][0]
        self.commit(self.decide(row, "transfer", other_account_id=self.savings))
        report = dashboard(self.db.ledger(), "ALL", TODAY)
        self.assertEqual({item["id"]: (item["opened_on"], item["balance"]) for item in report["accounts"]}, {
            self.checking: ("2026-11-16", "45000"), self.savings: ("2026-11-16", "55000"),
        })

    def test_review_prompt_sends_editable_fields_and_never_writes_to_the_ledger(self):
        category = self.db.save_category({"kind": "expense", "name": "Groceries", "color": "mint"})
        data = {
            "source": "csv", "account_id": self.checking, "prompt": "Use 16-11-2026 for missing dates.",
            "openai_model": "gpt-5.4-mini", "api_key": "sk-synthetic-edit-test-key",
            "rows": [{"id": 3, "name": "Market", "posted_on": "", "amount": "-12.50", "token": "private-import-token"}],
        }
        suggested = {"updates": [{"rows": [3], "posted_on": "2026-11-16"}], "sort": None, "message": ""}
        with patch("apps.web.imports.suggest_review_edits", return_value=suggested) as suggest:
            with self.assertRaisesRegex(ApiError, "Confirm that Quant may send"):
                self.api("/review-prompt", data)
            suggest.assert_not_called()
            result = self.api("/review-prompt", dict(data, ai_consent=True))
        prompt, context = suggest.call_args.args
        self.assertEqual(prompt, data["prompt"])
        self.assertEqual(context["rows"], [{"id": 3, "name": "Market", "posted_on": "", "amount": "-12.50"}])
        self.assertEqual([(item["id"], item["name"]) for item in context["categories"]], [(category, "Groceries")])
        self.assertEqual(suggest.call_args.kwargs, {"api_key": data["api_key"], "model": data["openai_model"]})
        self.assertEqual(result["updates"], suggested["updates"])
        self.assertEqual(self.db.ledger().entries, [])
        self.assertEqual(self.api("/setup")["history"], [])

    def test_category_suggestions_match_merchant_variants_and_flag_disagreement(self):
        groceries = self.db.save_category({"kind": "expense", "name": "Groceries", "color": "mint"})
        transport = self.db.save_category({"kind": "expense", "name": "Travel & transport", "color": "sky"})
        for name, category, account in (
            ("DEKAMARKT LOC 686 ZAANDAM NLD", groceries, self.savings),
            ("Uber", transport, self.checking), ("Uber", groceries, self.checking),
            ("SUMUP", groceries, self.checking),
        ):
            self.db.save_entry({"kind": "expense", "account_id": account, "name": name,
                                "posted_on": "2026-11-01", "amount": "9", "category_id": category}, TODAY)
        rows = self.preview(self.input("Date,Description,Amount\n"
            "2026-11-15,DEKAMARKT LOC 950 AMSTERDAM NLD,-15\n2026-11-15,Uber,-8\n"
            "2026-11-15,Uber Eats,-10\n2026-11-15,SUMUP,-11\n"))["rows"]
        self.assertEqual([row["suggested_category_id"] for row in rows], [groceries, None, None, None])
        self.assertEqual([row["category_conflict"] for row in rows], [False, True, False, False])
        self.commit(self.decide(rows[0], category_id=str(groceries)), self.decide(rows[1], category_id=""))
        self.assertIsNone(self.db.ledger().entries[-1]["category_id"])

    def test_openai_categories_use_only_relevant_history_and_accept_compatible_choices(self):
        transport = self.db.save_category({"kind": "expense", "name": "Travel", "color": "sky"})
        food = self.db.save_category({"kind": "expense", "name": "Dining", "color": "mint"})
        income = self.db.save_category({"kind": "income", "name": "Salary", "color": "rose"})
        for name, category in (("Uber ride", transport), ("Uber Eats", food), ("Unrelated", transport),
                               ("Conflicting merchant", food), ("Conflicting merchant", transport)):
            self.db.save_entry({"kind": "expense", "account_id": self.checking, "name": name,
                                "posted_on": "2026-11-01", "amount": "3.25", "category_id": category}, TODAY)
        data = {"account_id": self.checking, "api_key": "sk-synthetic-category-test-key", "openai_model": "gpt-5.4-mini",
                "rows": [{"id": 1, "name": "Uber Amsterdam", "amount": "-42", "token": "private"},
                         {"id": 2, "name": "Ambiguous", "amount": "-21"},
                         {"id": 3, "name": "Uber ride", "amount": "-42"},
                         {"id": 4, "name": "Conflicting merchant", "amount": "-12"}]}
        edits = {"updates": [{"rows": [1], "category_id": str(transport)}, {"rows": [2], "category_id": str(income)},
                             {"rows": [4], "category_id": str(food)}, {"rows": None, "category_id": str(food)}]}
        with patch("apps.web.imports.suggest_review_edits", return_value=edits) as suggest:
            with self.assertRaisesRegex(ApiError, "Confirm that Quant may send"):
                self.api("/review-categories", data)
            suggest.assert_not_called()
            result = self.api("/review-categories", dict(data, ai_consent=True))
        context = suggest.call_args.args[1]
        self.assertEqual([row["id"] for row in context["rows"]], [1, 2])
        self.assertEqual(set(context["rows"][0]), {"id", "name", "kind", "examples"})
        self.assertCountEqual(context["rows"][0]["examples"], [
            {"name": "Uber ride", "category_id": transport}, {"name": "Uber Eats", "category_id": food},
        ])
        self.assertEqual(context["rows"][1]["examples"], [])
        self.assertEqual(result["suggestions"], [{"id": 3, "category_id": transport}, {"id": 1, "category_id": transport}])
        self.assertEqual(len(self.db.ledger().entries), 5)
        self.assertEqual(self.api("/setup")["history"], [])

    def test_import_defaults_create_and_reuse_categories_and_allow_overrides(self):
        groceries = self.db.save_category({"kind": "expense", "name": "Groceries", "color": "mint"})
        other_income = self.db.save_category({"kind": "income", "name": "Other income", "color": "sky"})
        self.db.save_entry({"kind": "expense", "name": "Market", "amount": "20", "category_id": groceries,
                            "account_id": self.checking, "posted_on": "2026-11-01"}, TODAY)
        rows = self.preview(self.input("Date,Description,Amount\n2026-11-16,Market,-15\n2026-11-16,Unknown,-1\n"
                                       "2026-11-17,Refund,3\n2026-11-17,No category,-4\n"))["rows"]
        self.assertEqual(rows[0]["suggested_category_id"], groceries)
        with self.assertRaises(ApiError):
            self.commit(self.decide(rows[1]), self.decide(rows[2], amount="invalid"))
        self.assertEqual(len(self.db.ledger().categories), 2)
        self.commit(self.decide(rows[0], category_id=str(groceries)), self.decide(rows[1]),
                    self.decide(rows[2], category_id="default"), self.decide(rows[3], category_id=""))
        ledger = self.db.ledger()
        defaults = [item for item in ledger.categories if item["name"] == "Other expenses"]
        self.assertEqual(len(defaults), 1)
        categories = {entry["name"]: entry["category_id"] for entry in ledger.entries}
        self.assertEqual(categories, {"Market": groceries, "Unknown": defaults[0]["id"], "Refund": other_income, "No category": None})
        later = self.preview(self.input("Date,Description,Amount\n2026-11-19,Unknown,-6\n"))["rows"][0]
        self.assertIsNone(later["suggested_category_id"])
        self.commit(self.decide(later))
        self.assertEqual(len(self.db.ledger().categories), 3)

    def test_bank_statement_approval_links_existing_expense_without_changing_it(self):
        category = self.db.save_category({"name": "Coffee shops", "color": "amber", "kind": "expense"})
        entry = self.db.save_entry({"kind": "expense", "name": "Caffè Corner", "amount": "8.70",
                                    "account_id": self.checking, "posted_on": "2026-11-17",
                                    "statement_expected": True, "labels": ["work"], "category_id": category,
                                    "note": "Meeting with a client"}, TODAY)
        document, _ = save_attachment(self.db, entry, {"name": "receipt.pdf", "file": self.input(b"%PDF-test")["file"]}, TODAY)
        before = self.db.ledger().entries
        row = self.preview(self.input("Date,Description,Amount\n2026-11-17,POS CAFFÈ CORNER,-8.70\n"))["rows"][0]
        self.assertEqual((row["auto_match"], row["review_group"]), (entry, "ready"))
        for decision in (self.decide(row), self.decide(row, "link", entry_id=entry + 1),
                         self.decide(row, "link", entry_id=entry, name="Caffè Corner")):
            with self.assertRaisesRegex(ApiError, "no longer ready"):
                self.commit(decision, ready_only=True)
        decision = self.decide(row, "link", entry_id=entry)
        result = self.commit(decision, ready_only=True)
        self.assertEqual(result["rows"], [{"entry_id": entry, "action": "link", "existing": False}])
        self.assertEqual(self.db.ledger().entries, before)
        self.assertEqual(list_attachments(self.db, entry), [document])
        self.assertEqual(self.commit(decision, ready_only=True)["already"], 1)

    def test_bank_matches_exclude_opted_out_expenses_and_cash_defaults(self):
        wallet = self.account("Wallet", "cash")
        for account, overrides in ((self.checking, {"statement_expected": False}), (wallet, {})):
            entry = self.db.save_entry({"kind": "expense", "name": "Market", "amount": "8",
                                        "account_id": account, "posted_on": "2026-11-17", **overrides}, TODAY)
            row = self.preview(self.input("Date,Description,Amount\n2026-11-17,Market,-8\n", account_id=account))["rows"][0]
            self.assertEqual((row["matches"], row["auto_match"]), ([], None))
            with self.assertRaisesRegex(ApiError, "no longer matches"):
                self.commit(self.decide(row, "link", entry_id=entry))

    def test_siblings_and_similar_descriptions_require_manual_review(self):
        entry = self.db.save_entry({"kind": "expense", "name": "Market", "amount": "8",
                                    "account_id": self.checking, "posted_on": "2026-11-17"}, TODAY)
        same_amount = self.preview(self.input("Date,Description,Amount,Reference\n"
                                              "2026-11-17,Market,-8,BANK-1\n2026-11-18,Deli,-8,BANK-2\n"))["rows"]
        different_store = self.preview(self.input("Date,Description,Amount\n2026-11-17,Market North,-8\n"))["rows"][0]
        for row in (*same_amount, different_store):
            self.assertEqual((row["auto_match"], row["review_group"], row["matches"][0]["id"]), (None, "matches", entry))
            with self.assertRaisesRegex(ApiError, "no longer ready"):
                self.commit(self.decide(row, "link", entry_id=entry), ready_only=True)
        self.assertEqual(self.commit(self.decide(different_store, "link", entry_id=entry))["saved"], 1)

    def test_approved_bank_links_recheck_competing_commitments(self):
        expense = {"kind": "expense", "name": "Market", "amount": "8", "account_id": self.checking,
                   "posted_on": "2026-11-17"}
        entry = self.db.save_entry(expense, TODAY)
        rows = self.preview(self.input("Date,Description,Amount\n2026-11-16,Fare,-2\n2026-11-17,Market,-8\n"))["rows"]
        decisions = self.decide(rows[0]), self.decide(rows[1], "link", entry_id=entry)
        save_rule(self.db, {"name": "Market delivery", "kind": "expense", "cadence": "monthly",
                            "start_on": "2026-11-17", "amount": "8", "account_id": self.checking})
        with self.assertRaisesRegex(ApiError, "no longer ready"):
            self.commit(*decisions, ready_only=True)
        self.assertEqual(len(self.db.ledger().entries), 1)
        self.assertEqual(self.api("/setup")["history"], [])

    def test_independent_previews_cannot_bulk_link_same_expense_twice(self):
        entry = self.db.save_entry({"kind": "expense", "name": "Market", "amount": "8",
                                    "account_id": self.checking, "posted_on": "2026-11-17"}, TODAY)
        rows = [self.preview(self.input("Date,Description,Amount,Reference\n2026-11-17,Market,-8," + ref))["rows"][0]
                for ref in ("BANK-1", "BANK-2")]
        self.assertEqual([row["auto_match"] for row in rows], [entry, entry])
        decisions = [self.decide(row, "link", entry_id=entry) for row in rows]
        with self.assertRaisesRegex(ApiError, "no longer ready"):
            self.commit(*decisions, ready_only=True)

        def approve(decision):
            try:
                return self.commit(decision, ready_only=True)["saved"]
            except ApiError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(approve, decisions)), [1, 409])
        self.assertEqual(len(self.db.ledger().entries), 1)
        self.assertEqual(len(self.api("/setup")["history"]), 1)

    def test_bulk_approval_rejects_edits_and_late_matches_atomically(self):
        data = self.input("Date,Description,Amount\n2026-11-17,Fuel,-12.00\n2026-11-18,Sports,-20.00\n")
        rows = self.preview(data)["rows"]
        self.assertEqual([row["review_group"] for row in rows], ["ready", "ready"])
        with self.assertRaisesRegex(ApiError, "no longer ready"):
            self.commit(self.decide(rows[0], amount="-13.00"), ready_only=True)
        self.db.save_entry({"kind": "expense", "name": "Sports", "amount": "20.00",
                            "account_id": self.checking, "posted_on": "2026-11-18"}, TODAY)
        with self.assertRaisesRegex(ApiError, "no longer ready"):
            self.commit(*(self.decide(row) for row in rows), ready_only=True)
        self.assertEqual([entry["name"] for entry in self.db.ledger().entries], ["Sports"])

    def test_table_commit_accepts_edits_and_revalidates_unchanged_rows_atomically(self):
        original = {"kind": "expense", "name": "City Market", "amount": "20",
                    "account_id": self.checking, "posted_on": "2026-11-18"}
        entry = self.db.save_entry(original, TODAY)
        rows = self.preview(self.input(
            "Date,Description,Amount\n2026-11-17,Fuel,-12\n2026-11-18,City Market,-20\n"
        ))["rows"]
        self.assertEqual(rows[1]["auto_match"], entry)
        edited = self.decide(rows[0], name="Fuel receipt", amount="-13.50")
        verified = self.decide(rows[1], "link", entry_id=entry, verify_ready=True)
        self.db.save_entry(dict(original, name="Different shop"), TODAY, entry)
        with self.assertRaisesRegex(ApiError, "no longer ready"):
            self.commit(edited, verified)
        self.assertEqual(len(self.db.ledger().entries), 1)
        self.assertEqual(self.api("/setup")["history"], [])

        self.db.save_entry(original, TODAY, entry)
        result = self.commit(edited, verified)
        self.assertEqual([row["action"] for row in result["rows"]], ["create", "link"])
        self.assertEqual(len(self.api("/setup")["history"]), 1)
        self.assertEqual(self.commit(edited, verified)["already"], 2)
        self.assertCountEqual([row["name"] for row in self.db.ledger().entries], ["City Market", "Fuel receipt"])

    def test_ready_import_links_manual_expense_with_its_invoice_and_adds_fresh_row(self):
        category = self.db.save_category({"kind": "expense", "name": "Internet", "color": "mint"})
        transaction = self.db.save_entry({
            "kind": "expense", "name": "Bluebird internet", "amount": "78",
            "account_id": self.checking, "posted_on": "2026-11-15",
            "category_id": category, "labels": ["Home"], "note": "Contract invoice",
        }, TODAY)
        invoice = b"%PDF-1.4\nExample invoice\n"
        document, _ = save_attachment(self.db, transaction, {
            "name": "November.pdf", "file": base64.b64encode(invoice).decode(),
        }, TODAY)
        statement = self.input("Date,Description,Amount\n2026-11-15,SEPA BLUEBIRD INTERNET,-78\n2026-11-16,Transit,-4\n")
        rows = self.preview(statement)["rows"]
        self.assertEqual((rows[0]["review_group"], rows[0]["auto_match"]), ("ready", transaction))
        other = self.preview(dict(statement, account_id=self.savings))["rows"][0]
        self.assertIsNone(other["auto_match"])
        decisions = [self.decide(rows[0], "link", entry_id=transaction), self.decide(rows[1])]
        result = self.commit(*decisions, ready_only=True)
        self.assertEqual([item["action"] for item in result["rows"]], ["link", "create"])
        self.assertEqual(self.commit(*decisions, ready_only=True)["already"], 2)
        ledger = self.db.ledger()
        self.assertEqual(len(ledger.entries), 2)
        manual = ledger.entries[0]
        self.assertEqual((manual["category_id"], manual["labels"], manual["note"]), (category, ["Home"], "Contract invoice"))
        self.assertEqual(download_attachment(self.db, transaction, document["id"]).body, invoice)

    def test_statement_opt_out_and_same_amount_rows_do_not_auto_match(self):
        for name, amount, expected in (("Vet clinic", "20", False), ("Green market", "15", True)):
            self.db.save_entry({"kind": "expense", "name": name, "amount": amount, "account_id": self.checking,
                                "posted_on": "2026-11-15", "statement_expected": expected}, TODAY)
        rows = self.preview(self.input(
            "Date,Description,Amount\n2026-11-15,Vet clinic,-20\n2026-11-15,Green market,-15\n2026-11-16,Books,-15\n"
        ))["rows"]
        self.assertFalse(rows[0]["matches"])
        self.assertEqual(rows[1]["review_group"], "matches")
        self.assertTrue(all(row["auto_match"] is None for row in rows))

    def test_flag_changed_after_preview_rejects_the_whole_ready_batch(self):
        manual = {"kind": "expense", "name": "Bluebird internet", "amount": "78", "account_id": self.checking,
                  "posted_on": "2026-11-15"}
        transaction = self.db.save_entry(manual, TODAY)
        rows = self.preview(self.input(
            "Date,Description,Amount\n2026-11-14,Transit,-4\n2026-11-15,Bluebird internet,-78\n"
        ))["rows"]
        self.assertEqual(rows[1]["auto_match"], transaction)
        self.db.save_entry(dict(manual, statement_expected=False), TODAY, transaction)
        with self.assertRaisesRegex(ApiError, "no longer ready"):
            self.commit(self.decide(rows[0]), self.decide(rows[1], "link", entry_id=transaction), ready_only=True)
        self.assertEqual(len(self.db.ledger().entries), 1)

    def test_csv_mapping_decimal_and_repeated_identical_rows_are_idempotent(self):
        raw = self.input("Datum;Naam / Omschrijving;Af Bij;Bedrag (EUR)\n"
                         "20261115;Lunch;Af;1.200,30\n20261116;Interest;Bij;10,25\n"
                         "20261115;Lunch;Af;1.200,30\n")
        result = self.preview(raw)
        rows = result["rows"]
        self.assertEqual([row["amount"] for row in rows], ["-120030", "1025", "-120030"])
        selected = [self.decide(row) for row in rows]
        outcome = self.commit(*selected)
        self.assertEqual(outcome["saved"], 3)
        repeated = self.commit(*selected)
        self.assertEqual(repeated["already"], 3)
        self.assertEqual(len(self.db.ledger().entries), 3)
        self.assertEqual(self.api("/setup")["history"][0]["count"], 3)
        self.assertTrue(all(row["existing"] for row in self.preview(raw)["rows"]))

    def test_separate_amount_columns_and_ambiguous_dates_require_review(self):
        data = self.input("Date,Merchant,Debit,Credit\n03/11/2026,Cafe,5.25,\n12/11/2026,Salary,,15.00\n",
                          amount_mode="separate")
        self.assertTrue(all(row["errors"] for row in self.preview(data)["rows"]))
        rows = self.preview(dict(data, date_format="dmy"))["rows"]
        self.assertEqual([(row["posted_on"], row["amount"]) for row in rows], [
            ("2026-11-03", "-525"), ("2026-11-12", "1500"),
        ])

    def test_commit_is_atomic_on_invalid_date_and_token_tampering(self):
        rows = self.preview(self.input("Date,Description,Amount\n2026-11-13,First,-1\n2026-11-14,Second,-2\n"))["rows"]
        with self.assertRaisesRegex(ApiError, "Row 3"):
            self.commit(self.decide(rows[0]), self.decide(rows[1], posted_on="2026-11-21"))
        self.assertEqual(self.db.ledger().entries, [])
        self.assertEqual(self.api("/setup")["history"], [])
        with self.assertRaisesRegex(ApiError, "Review the file again"):
            self.commit(self.decide(rows[0], token=rows[0]["token"] + "x"))

    def test_csv_marks_foreign_currency_and_cannot_force_post(self):
        result = self.preview(self.input("Date,Description,Amount,Currency\n2026-11-13,Dollar store,-3.00,USD\n"))
        row = result["rows"][0]
        self.assertTrue(row["blocked"])
        with self.assertRaisesRegex(ApiError, "Only rows in euros"):
            self.commit(self.decide(row))

    def test_transfer_counterpart_reconciles_without_changing_wealth(self):
        out = self.preview(self.input("Date,Description,Amount\n2026-11-17,Move savings,-100\n"))["rows"][0]
        stored = self.commit(self.decide(out, "transfer", other_account_id=self.savings))["rows"][0]["entry_id"]
        incoming = self.preview(self.input("Date,Description,Amount\n2026-11-18,Cash transfer,100\n",
                                            account_id=self.savings))["rows"][0]
        self.assertEqual(incoming["matches"][0]["id"], stored)
        self.commit(self.decide(incoming, "link", entry_id=stored))
        overview = dashboard(self.db.ledger(), "1M", TODAY)["summary"]
        self.assertEqual({key: overview[key] for key in ("wealth", "expense", "cash", "savings")},
                         {"wealth": "100000", "expense": "0", "cash": "40000", "savings": "60000"})
        self.assertEqual(len(self.db.ledger().entries), 1)

    def test_match_commitment_and_already_booked_shared_cash(self):
        rule = save_rule(self.db, {"name": "Subscription", "kind": "expense", "cadence": "monthly",
                                  "start_on": "2026-11-17", "amount": "12", "account_id": self.checking})
        row = self.preview(self.input("Date,Description,Amount\n2026-11-18,Subscription,-12\n"))["rows"][0]
        self.assertEqual(row["commitments"][0]["id"], rule)
        saved = self.commit(self.decide(row, "commitment", rule_id=rule, due_on="2026-11-17"))["rows"][0]
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT entry_id FROM commitment_occurrences").fetchone()[0], saved["entry_id"])
        person = self.api("/people", {"name": "Lo", "tag": "lo"}, shared=True)["id"]
        self.api("/rules", {"name": "Shared dinner", "amount": "50", "first_on": "2026-11-18",
                            "account_id": self.checking, "participants": [person], "record_now": True}, shared=True)
        shared = self.preview(self.input("Date,Description,Amount\n2026-11-18,Dinner,-50\n"))["rows"][0]
        self.assertLess(shared["matches"][0]["id"], 0)
        self.assertEqual((shared["matches"][0]["amount"], shared["matches"][0]["personal_amount"],
                          shared["matches"][0]["shared_delta"]), ("-5000", "2500", "2500"))
        self.commit(self.decide(shared, "link", entry_id=shared["matches"][0]["id"]))
        self.assertEqual(len(self.db.ledger().entries), 2)

    def test_scheduled_shared_reimbursement_can_be_recorded_from_import(self):
        person = self.api("/people", {"name": "Lo", "tag": "lo"}, shared=True)["id"]
        self.api("/rules", {"name": "Shared dinner", "amount": "50", "first_on": "2026-11-18",
                            "account_id": self.checking, "participants": [person], "record_now": True}, shared=True)
        plan = self.api("/expected", {"person_id": person, "account_id": self.checking, "amount": "25",
                                     "direction": "receive", "due_on": "2026-11-21"}, shared=True)["id"]
        receipt = self.preview(self.input("Date,Description,Amount\n2026-11-20,Repayment,25\n"))["rows"][0]
        self.assertEqual(receipt["expected"][0]["id"], plan)
        entry = self.commit(self.decide(receipt, "expected", expected_id=plan))["rows"][0]
        self.assertLess(entry["entry_id"], 0)
        overview = dashboard(self.db.ledger(), "1M", TODAY)
        self.assertEqual(overview["shared_balance"], "0")
        self.assertEqual(overview["summary"]["income"], "0")

    def test_amazon_portability_repeated_order_total_is_once_and_linked(self):
        bank_entry = self.db.save_entry({"kind": "expense", "name": "AMAZON EU", "amount": "34.56",
                                        "account_id": self.checking, "posted_on": "2026-11-16",
                                        "statement_expected": False}, TODAY)
        item = {"orderId": "123-123", "orderDate": "2026-11-13T12:30:00Z", "currencyCode": "EUR",
                "totalOwed": 34.56, "productName": "Book"}
        raw = self.input(json.dumps([item, {**item, "productName": "Pen"}]), "amazon")
        rows = self.preview(raw)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], "-3456")
        self.assertEqual(rows[0]["matches"][0]["id"], bank_entry)
        batch = self.commit(self.decide(rows[0], "link", entry_id=bank_entry))["batch_id"]
        self.assertEqual(len(self.db.ledger().entries), 1)
        receipt = self.api("/batch/" + str(batch))["rows"][0]
        self.assertEqual((receipt["reference"], receipt["amount"], receipt["action"]), ("123-123", "-3456", "link"))
        self.assertIn("Book / Pen", receipt["name"])

    def test_zip_retail_export_leaves_uncertain_totals_for_manual_confirmation(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Retail.OrderHistory/Retail.OrderHistory.1.csv",
                            "Order ID,Order Date,Currency,Total Owed,Product Name,Order Status\n"
                            "1,2026-11-13,EUR,7.00,Book,Shipped\n"
                            "1,2026-11-13,EUR,7.00,Pen,Shipped\n"
                            "2,2026-11-13,EUR,1.00,Other,Cancelled\n")
            output.writestr("DigitalOrders.csv", "Unrelated,columns\nX,Y\n")
        rows = self.preview(self.input(archive.getvalue(), "amazon"))["rows"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["amount"])
        self.assertTrue(rows[0]["errors"])
        self.commit(self.decide(rows[0], amount="-14.00"))
        self.assertEqual(self.db.ledger().entries[0]["amount"], 1400)

    def test_pdf_amount_and_balances_are_never_interchanged(self):
        _, rows, ambiguous = _pdf_rows("2026-11-19 Grocery store -20.00 1000.00\n"
                                       "2026-11-20 Coffee -2.00\n"
                                       "2026-11-20 Closing balance 998.00")
        self.assertEqual(ambiguous, [1])
        self.assertEqual(rows, [(2, ["2026-11-20", "Coffee", "-2.00", ""])])
        self.assertEqual(parse_amount("EUR (2.345,67)"), -234567)

    def test_unnamed_local_pdf_bill_uses_the_batch_name_and_stays_deduplicated(self):
        receipt = self.input(b"%PDF-synthetic", "pdf")
        with patch("apps.web.imports._pdf_text", return_value="Payment date: 2026-11-17\nTotal paid: 24.50"):
            preview = self.preview(receipt)
            self.assertEqual((preview["name"], preview["rows"][0]["name"]), ("Bill - 2026-11-20", "Bill - 2026-11-20"))
            self.commit(self.decide(preview["rows"][0]))
            renamed = self.preview(dict(receipt, name="November phone bill"))
        self.assertEqual(renamed["rows"][0]["review_group"], "handled")
        self.assertEqual(self.api("/setup")["history"][0]["name"], preview["name"])

    def test_batch_name_migration_preserves_legacy_import_history(self):
        legacy = Database(self.db.path.with_name("legacy.sqlite3"))
        with legacy.connection() as conn:
            conn.executescript("""
                CREATE TABLE import_batches (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, account_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, count INTEGER NOT NULL
                );
                INSERT INTO import_batches(id, source, account_id, count) VALUES (1, 'pdf', 1, 3);
            """)
        legacy.initialize()
        initialize_imports(legacy)
        with legacy.connection() as conn:
            prior = conn.execute("SELECT source, count, name FROM import_batches WHERE id = 1").fetchone()
        self.assertEqual(tuple(prior), ("pdf", 3, None))

    @unittest.skipUnless(_pdf_backend(), "PDF extraction requires Poppler or macOS Swift")
    def test_actual_pdf_extractor_and_scanned_fallback(self):
        raw = self.input(sample_pdf(b"2026-11-17 Coffee -2.50"), "pdf")
        rows = self.preview(raw)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], "-250")
        scanned = self.preview(self.input(sample_pdf(b""), "pdf"))
        self.assertEqual(scanned["rows"], [])
        self.assertIn("No transaction text", scanned["notices"][0])
