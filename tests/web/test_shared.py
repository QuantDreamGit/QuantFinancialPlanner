import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from apps.web.reports import dashboard, planner_inputs, transaction_list
from apps.web.shared import handle_shared, payment_dates, projected_cash
from apps.web.storage import Database
from apps.web.values import ApiError


TODAY = date(2026, 11, 19)


class SharedPaymentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "test.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()
        self.account = self.db.save_account({
            "name": "Checking", "kind": "checking", "opening_balance": "1000", "opened_on": "2026-05-01",
        }, TODAY)
        self.category = self.db.save_category({
            "name": "Utilities", "kind": "expense", "color": "sky",
        })
        self.alice = self.action("/people", {"name": "Alice", "tag": "@alice"})["id"]
        self.bob = self.action("/people", {"name": "Bob", "tag": "@bob"})["id"]

    def action(self, resource="", data=None, method="POST", today=TODAY):
        handler = SimpleNamespace(server=SimpleNamespace(database=self.db), read_data=lambda _: data)
        result, status = handle_shared(handler, "/api/shared" + resource, {}, method, today)
        self.assertIn(status, (200, 201))
        return result

    def view(self):
        return self.action(method="GET")

    def rule(self, **overrides):
        return {
            "name": "Internet", "amount": "100.01", "first_on": "2026-11-08",
            "frequency": "once", "account_id": self.account, "category_id": self.category,
            "participants": [self.bob, self.alice], "record_now": True, "labels": ["household"],
            **overrides,
        }

    def settle(self, **overrides):
        return {
            "person_id": self.alice, "account_id": self.account, "posted_on": "2026-11-11",
            "amount": "33.34", "direction": "receive", **overrides,
        }

    def test_equal_split_uses_exact_cents_and_reconciles_all_ledger_views(self):
        request = self.rule(request_key="first-internet")
        item = self.action("/rules", request)["id"]
        self.assertEqual(self.action("/rules", request)["id"], item)
        with self.assertRaises(ApiError) as conflicting:
            self.action("/rules", {**request, "amount": "120"})
        self.assertEqual(conflicting.exception.status, 409)
        before = dashboard(self.db.ledger(), "1M", TODAY)
        self.assertEqual(before["summary"]["expense"], "3334")
        self.assertEqual(before["categories"][0]["amount"], "3334")
        self.assertEqual(before["summary"]["cash"], "89999")
        self.assertEqual(before["summary"]["wealth"], "96666")
        self.assertEqual(before["shared_balance"], "6667")
        self.assertEqual(self.view()["owed"], "6667")
        balances = {person["tag"]: person["balance"] for person in self.view()["people"]}
        self.assertEqual(balances, {"alice": "3334", "bob": "3333"})

        settlement = self.settle(request_key="receive-alice")
        saved = self.action("/settlements", settlement)
        self.assertEqual(self.action("/settlements", settlement), saved)
        after = dashboard(self.db.ledger(), "1M", TODAY)
        self.assertEqual(after["summary"]["cash"], "93333")
        self.assertEqual(after["summary"]["wealth"], before["summary"]["wealth"])
        self.assertEqual(after["summary"]["expense"], "3334")
        self.assertEqual(after["summary"]["income"], "0")
        self.assertEqual(after["shared_balance"], "3333")
        self.assertEqual(planner_inputs(self.db.ledger(), TODAY)["startingCash"], "933.33")
        results = transaction_list(self.db.ledger(), page=1, size=10, search="@alice")
        self.assertEqual({entry["kind"] for entry in results["entries"]}, {"expense", "settlement"})
        self.assertEqual(results["total"], 2)
        self.assertTrue(all(entry["id"] < 0 for entry in results["entries"]))
        self.assertEqual(self.view()["owed"], "3333")

    def test_external_payer_changes_wealth_and_settlement_changes_cash(self):
        self.action("/rules", self.rule(amount="160", payer_id=self.alice, split="custom", shares={
            "me": "25", str(self.alice): "90", str(self.bob): "45",
        }))
        data = dashboard(self.db.ledger(), "1M", TODAY)
        self.assertEqual(data["summary"]["cash"], "100000")
        self.assertEqual(data["summary"]["wealth"], "97500")
        self.assertEqual(data["shared_balance"], "-2500")
        self.assertEqual(data["summary"]["expense"], "2500")
        self.assertEqual(self.view()["owe"], "2500")
        with self.assertRaisesRegex(ApiError, "outstanding balance"):
            self.action("/settlements", self.settle(amount="25.01", direction="pay"))
        self.action("/settlements", self.settle(amount="25", direction="pay"))
        closing = dashboard(self.db.ledger(), "1M", TODAY)
        self.assertEqual(closing["summary"]["cash"], "97500")
        self.assertEqual(closing["summary"]["wealth"], "97500")
        self.assertEqual(closing["summary"]["expense"], "2500")
        self.assertEqual(self.view()["owe"], "0")

    def test_unposted_recurrences_are_cash_commitments_until_booked_or_skipped(self):
        data = self.rule(amount="30", first_on="2027-01-31", end_on="2027-04-30", frequency="monthly", record_now=False)
        rule_id = self.action("/rules", data)["id"]
        path = "/rules/" + str(rule_id)
        start, end = date(2027, 1, 1), date(2027, 5, 1)

        def forecast():
            return [(entry["date"].isoformat(), entry["amount"]) for entry in projected_cash(self.db, start, end)]

        self.assertEqual(forecast(), [
            ("2027-01-31", -3000), ("2027-02-28", -3000), ("2027-03-31", -3000), ("2027-04-30", -3000),
        ])
        first = projected_cash(self.db, start, end)[0]
        self.assertEqual((first["amount"], first["personal_amount"], first["owed_back"]), (-3000, "1000", "2000"))
        self.assertEqual(self.db.ledger().entries, [])
        posted = {"due_on": "2027-01-31", "posted_on": "2027-01-30"}
        occurrence = self.action(path + "/record", posted, today=date(2027, 1, 31))
        self.assertEqual(self.action(path + "/record", posted, today=date(2027, 1, 31)), occurrence)
        self.action(path + "/skip", {"due_on": "2027-02-28"})
        self.action(path, {**data, "amount": "60"}, "PUT")
        self.assertEqual(forecast(), [("2027-03-31", -6000), ("2027-04-30", -6000)])
        self.assertEqual(self.db.ledger().entries[0]["cash_delta"], -3000)
        self.assertEqual(self.db.ledger().entries[0]["amount"], 1000)
        self.assertEqual(self.view()["history"][0]["shares"],
                         [{"member": "You", "amount": "1000"}, {"member": "@alice", "amount": "1000"},
                          {"member": "@bob", "amount": "1000"}])
        with self.assertRaisesRegex(ApiError, "change its starting date"):
            self.action(path, {**data, "first_on": "2027-01-01"}, "PUT")
        self.action(path + "/stop", {})
        self.assertEqual(forecast(), [])
        self.assertEqual(len(self.db.ledger().entries), 1)

    def test_weekly_and_annual_schedules_keep_the_original_anchor(self):
        self.assertEqual(list(payment_dates(
            {"first_on": "2024-02-29", "end_on": None, "frequency": "yearly"}, date(2025, 1, 1), date(2028, 3, 1)
        )), [date(2025, 2, 28), date(2026, 2, 28), date(2027, 2, 28), date(2028, 2, 29)])
        self.assertEqual(list(payment_dates(
            {"first_on": "2026-11-01", "end_on": "2026-11-19", "frequency": "weekly"},
            date(2026, 11, 2), date(2026, 11, 30)
        )), [date(2026, 11, 8), date(2026, 11, 15)])

    def test_scheduled_reimbursement_is_replaced_by_actual_cash(self):
        self.action("/rules", self.rule())
        data = self.settle(amount="30", due_on="2026-11-22", request_key="expected")
        expected = self.action("/expected", data)["id"]
        self.assertEqual(self.action("/expected", data)["id"], expected)
        self.assertEqual(self.view()["owed"], "6667")
        with self.assertRaisesRegex(ApiError, "balance available to schedule"):
            self.action("/expected", dict(data, amount="4", request_key="extra"))
        with self.db.connection(snapshot=True) as connection:
            events = projected_cash(self.db, date(2026, 11, 20), date(2026, 11, 30), conn=connection)
        self.assertEqual([(row["date"], row["amount"], row["kind"]) for row in events],
                         [(date(2026, 11, 22), 3000, "settlement")])
        endpoint = f"/expected/{expected}/record"
        confirmation = {"posted_on": "2026-11-22"}
        posted = self.action(endpoint, confirmation, today=date(2026, 11, 22))
        self.assertEqual(self.action(endpoint, confirmation, today=date(2026, 11, 22)), posted)
        self.assertEqual(projected_cash(self.db, date(2026, 11, 20), date(2026, 11, 30)), [])
        self.assertEqual(self.view()["expected"], [])
        self.assertEqual(dashboard(self.db.ledger(), "1M", date(2026, 11, 22))["summary"]["cash"], "92999")

    def test_manual_settlement_suspends_underfunded_planned_payment(self):
        self.action("/rules", self.rule(amount="30", payer_id=self.alice))
        self.action("/expected", self.settle(amount="7", direction="pay", due_on="2026-11-22"))
        horizon = (date(2026, 11, 20), date(2026, 11, 30))
        self.assertEqual([event["amount"] for event in projected_cash(self.db, *horizon)], [-700])
        self.action("/settlements", self.settle(amount="4", direction="pay"))
        self.assertEqual(projected_cash(self.db, *horizon), [])
        pending = self.view()["expected"][0]
        self.assertFalse(pending["funded"])
        self.action(f'/expected/{pending["id"]}/cancel', {})
        self.assertEqual(self.view()["expected"], [])

    def test_void_and_reopen_keep_a_visible_history(self):
        self.action("/rules", self.rule())
        payout = self.action("/settlements", self.settle())["id"]
        self.action(f"/settlements/{payout}/void", {})
        self.assertEqual(self.view()["owed"], "6667")
        posting = next(item["id"] for item in self.view()["history"] if item["type"] == "payment")
        self.action(f"/postings/{posting}/void", {})
        self.db = Database(self.path)
        self.db.initialize()
        result = self.view()
        self.assertEqual(result["owed"], "0")
        self.assertEqual(len(result["history"]), 2)
        self.assertTrue(all(item["voided_at"] for item in result["history"]))
        self.assertEqual(self.db.ledger().entries, [])

    def test_bad_split_or_early_account_date_rejects_without_side_effects(self):
        with self.assertRaisesRegex(ApiError, "add up"):
            self.action("/rules", self.rule(split="custom", shares={
                "me": "0", str(self.alice): "40", str(self.bob): "40",
            }))
        with self.assertRaisesRegex(ApiError, "account opening date"):
            self.action("/rules", self.rule(first_on="2026-01-01"))
        with self.assertRaisesRegex(ApiError, "earlier calendar date"):
            self.action("/rules", self.rule(first_on="2027-01-01"))
        self.assertEqual(self.view()["rules"], [])
        self.assertEqual(self.db.ledger().entries, [])

    def test_contacts_are_searchable_and_history_survives_rename(self):
        self.action("/rules", self.rule())
        self.action(f"/people/{self.alice}", {"name": "Alice", "tag": "@al"}, "PUT")
        with self.assertRaises(ApiError) as duplicate:
            self.action("/people", {"name": "Another Alice", "tag": "@AL"})
        self.assertEqual(duplicate.exception.status, 409)
        result = transaction_list(self.db.ledger(), page=1, size=10, search="@alice")
        self.assertEqual(result["total"], 1)
        self.assertIn("@alice", result["entries"][0]["labels"])
        self.assertEqual(next(item for item in self.view()["people"] if item["tag"] == "al")["balance"], "3334")
