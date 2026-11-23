import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from apps.web.forecast import calendar_occurrences, post_occurrence, projected_events, runway
from apps.web.reports import dashboard
from apps.web.returns import end_return, handle_returns, monthly_amount, return_details, save_return
from apps.web.storage import Database
from apps.web.values import ApiError


class ReturnTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(Path(folder.name) / "returns.sqlite")
        self.db.initialize()
        self.today = date(2026, 11, 20)
        self.deposit = self.account("Term deposit", "savings", "1000.00")
        self.bond = self.account("Fixed coupon bond", "investment", "5000.00")
        self.checking = self.account("Checking", "checking", "300.00")
        self.interest = self.db.save_category({"name": "Interest", "kind": "income", "color": "mint"})

    def account(self, name, kind, opening, opened_on="2026-01-01"):
        return self.db.save_account({
            "name": name, "kind": kind, "opening_balance": opening, "opened_on": opened_on,
        }, self.today)

    def create(self, **changes):
        return save_return(self.db, {
            "name": "Monthly interest", "instrument_id": self.deposit, "category_id": self.interest,
            "mode": "fixed", "amount": "23.45", "start_on": "2026-11-20", **changes,
        })

    def row(self, item_id):
        return next(item for item in return_details(self.db, self.today)["returns"] if item["id"] == item_id)

    def test_rate_uses_integer_cents_and_rounds_half_cent_up(self):
        self.assertEqual(monthly_amount({"mode": "rate", "principal": "1", "rate": "0,500000"}), (1, 100, "0.5"))
        self.assertEqual(monthly_amount({"mode": "rate", "principal": "1234.56", "rate": "0.123456"}),
                         (152, 123456, "0.123456"))
        self.assertEqual(monthly_amount({"mode": "rate", "principal": "92233720368547758.07", "rate": "100"}),
                         (2**63 - 1, 2**63 - 1, "100"))
        for mode, fields, field in (
            ("rate", {"principal": "1", "rate": "0.000001"}, "rate"),
            ("rate", {"principal": "1", "rate": "0.1234567"}, "rate"),
            ("rate", {"principal": "1", "rate": "-0.5"}, "rate"),
            ("rate", {"principal": "0", "rate": "1"}, "principal"),
            ("rate", {"principal": "92233720368547758.07", "rate": "101"}, "rate"),
            ("fixed", {"amount": "0"}, "amount"),
        ):
            with self.subTest(mode=mode, fields=fields), self.assertRaises(ApiError) as failure:
                monthly_amount(dict(fields, mode=mode))
            self.assertEqual(failure.exception.field, field)

    def test_rate_is_constant_even_when_retained_balance_grows(self):
        item_id = self.create(mode="rate", principal="1000", rate="0.5", start_on="2026-09-30")
        rule_id = self.row(item_id)["commitment_id"]
        post_occurrence(self.db, {"rule_id": rule_id, "date": "2026-09-30"}, self.today)
        post_occurrence(self.db, {"rule_id": rule_id, "date": "2026-10-30"}, self.today)
        self.assertEqual([row["amount"] for row in self.db.ledger().entries], [500, 500])
        forecast = projected_events(self.db, self.today, date(2027, 1, 31))
        self.assertEqual([row["date"].isoformat() for row in forecast], ["2026-11-30", "2026-12-30", "2027-01-30"])
        self.assertEqual([row["amount"] for row in forecast], [500, 500, 500])
        self.assertEqual(dashboard(self.db.ledger(), "3M", self.today)["summary"]["savings"], "101000")

    def test_cross_account_return_only_credits_the_selected_payout(self):
        item_id = self.create(instrument_id=self.bond, payout_account_id=self.checking, product="fixed_income")
        rule_id = self.row(item_id)["commitment_id"]
        entry_id = post_occurrence(self.db, {"rule_id": rule_id, "date": self.today.isoformat()}, self.today)
        entry = self.db.ledger().entries[0]
        self.assertEqual((entry["id"], entry["kind"], entry["account_id"], entry["category_id"]),
                         (entry_id, "income", self.checking, self.interest))
        summary = dashboard(self.db.ledger(), "3M", self.today)["summary"]
        self.assertEqual((summary["cash"], summary["investments"], summary["income"]), ("32345", "500000", "2345"))
        reopened = Database(self.db.path)
        reopened.initialize()
        history = return_details(reopened, self.today)["returns"][0]
        self.assertEqual(history["instrument_id"], self.bond)
        self.assertEqual((history["posted_count"], history["recent"][0]["amount"]), (1, "2345"))

    def test_retained_investment_return_preserves_cash_runway_and_posts_income(self):
        item_id = self.create(instrument_id=self.bond, amount="25")
        rule = self.row(item_id)
        actual = runway(self.db, self.today, self.today)["days"][self.today.day - 1]
        self.assertEqual((actual["actual"], actual["projected"]), ("130000", "130000"))
        self.assertEqual(actual["items"][0]["purpose"], "return")
        post_occurrence(self.db, {"rule_id": rule["commitment_id"], "date": self.today.isoformat()}, self.today)
        summary = dashboard(self.db.ledger(), "3M", self.today)["summary"]
        self.assertEqual((summary["investments"], summary["cash"], summary["income"]), ("502500", "30000", "2500"))

    def test_month_end_skip_match_and_archive_preserve_the_recorded_credit(self):
        item_id = self.create(start_on="2026-01-31")
        rule_id = self.row(item_id)["commitment_id"]
        self.assertEqual([month for month in self.row(item_id)["due"][:3]],
                         ["2026-01-31", "2026-02-28", "2026-03-31"])
        self.assertEqual(self.row(item_id)["next_on"], "2026-11-30")
        payload = {"rule_id": rule_id, "date": "2026-01-31"}
        post_occurrence(self.db, dict(payload, skip=True), self.today)
        self.assertEqual(self.row(item_id)["due_on"], "2026-02-28")
        self.assertEqual(self.row(item_id)["skipped_count"], 1)
        matched = self.db.save_entry({
            "name": "Feb credit", "kind": "income", "account_id": self.deposit, "posted_on": "2026-02-28",
            "amount": "22.75", "category_id": self.interest,
        }, self.today)
        post_occurrence(self.db, {"rule_id": rule_id, "date": "2026-02-28", "entry_id": matched}, self.today)
        end_return(self.db, item_id, self.today)
        end_return(self.db, item_id, self.today)
        history = self.row(item_id)
        self.assertEqual(history["due_on"], "2026-03-31")
        self.assertEqual(history["archived_on"], "2026-11-20")
        self.assertIsNone(history["next_on"])
        self.assertEqual(history["recent"][0]["amount"], "2275")
        self.assertEqual(len(self.db.ledger().entries), 1)
        self.assertEqual(projected_events(self.db, self.today, date(2027, 3, 1)), [])
        self.assertEqual(calendar_occurrences(self.db, date(2026, 2, 1), date(2026, 2, 28))[0]["status"], "posted")

    def test_rejects_invalid_accounts_dates_and_category_without_orphan_commits(self):
        other = self.account("Unrelated broker", "investment", "0")
        late = self.account("Late bank", "checking", "0", "2026-11-20")
        expense = self.db.save_category({"name": "Expense", "kind": "expense", "color": "rose"})
        invalid = [
            ({"instrument_id": self.checking}, "instrument_id"),
            ({"payout_account_id": other}, "payout_account_id"),
            ({"payout_account_id": late, "start_on": "2026-11-19"}, "start_on"),
            ({"end_on": "2026-11-19"}, "end_on"),
            ({"category_id": expense}, "category_id"),
        ]
        for changes, field in invalid:
            with self.subTest(**changes), self.assertRaises(ApiError) as error:
                self.create(**changes)
            self.assertEqual(error.exception.field, field)
        with self.db.connection(write=True) as conn:
            conn.execute("""
                CREATE TRIGGER fail_return BEFORE INSERT ON investment_returns
                BEGIN SELECT RAISE(ABORT,'test failure'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.create()
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM commitments").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM investment_returns").fetchone()[0], 0)

    def test_handler_preserves_input_shape_and_honors_inclusive_end_date(self):
        data = {"name": "January only", "instrument_id": self.deposit, "mode": "fixed", "amount": "1.23",
                "start_on": "2027-01-01", "end_on": "2027-01-01", "product": "fixed_income"}
        handler = SimpleNamespace(database=self.db, read_data=lambda _size: data)
        result, status = handle_returns(handler, "/api/returns", {}, "POST", self.today)
        self.assertEqual(status, 201)
        listed, status = handle_returns(handler, "/api/returns", {}, "GET", self.today)
        self.assertEqual(status, 200)
        row = listed["returns"][0]
        self.assertEqual((row["id"], row["product"], row["mode"], row["amount"], row["next_on"], row["due"]),
                         (result["id"], "fixed_income", "fixed", "123", "2027-01-01", []))
        dates = [entry["date"].isoformat() for entry in projected_events(self.db, date(2027, 1, 1), date(2027, 2, 2))]
        self.assertEqual(dates, ["2027-01-01"])
