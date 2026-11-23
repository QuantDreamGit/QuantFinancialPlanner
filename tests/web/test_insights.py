import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from apps.web.insights import detective, handle_insights, month_window, replay, waterfall
from apps.web.reports import dashboard, transaction_list
from apps.web.shared import handle_shared
from apps.web.storage import Database
from apps.web.values import ApiError


TODAY = date(2026, 11, 19)


class InsightTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(Path(folder.name) / "ledger.sqlite3")
        self.db.initialize()
        self.cash = self.account("Checking", "checking", "1000", "2026-07-01")

    def account(self, name, kind, amount, opened):
        return self.db.save_account({
            "name": name, "kind": kind, "opening_balance": amount, "opened_on": opened,
        }, TODAY)

    def category(self, name, parent=None):
        return self.db.save_category({
            "name": name, "kind": "expense", "color": "rose", "parent_id": parent,
        })

    def entry(self, kind, on, name, amount, **extra):
        return self.db.save_entry(dict({
            "kind": kind, "name": name, "posted_on": on, "amount": amount, "account_id": self.cash,
        }, **extra), TODAY)

    def shared(self, path, data, method="POST"):
        handler = SimpleNamespace(server=SimpleNamespace(database=self.db), read_data=lambda size: data)
        return handle_shared(handler, "/api/shared/" + path, {}, method, TODAY)[0]["id"]

    def test_a_waterfall_reconciles_openings_gains_internal_transfers_and_shared_payments(self):
        investment = self.account("Broker", "investment", "200", "2026-11-01")
        savings = self.account("Savings", "savings", "100", "2026-11-02")
        alice = self.shared("people", {"name": "Alice", "tag": "alice"})
        payment = self.shared("rules", {
            "name": "Hotel", "frequency": "once", "amount": "100", "first_on": "2026-11-05",
            "account_id": self.cash, "participants": [alice],
        })
        self.shared(f"rules/{payment}/record", {"due_on": "2026-11-05", "posted_on": "2026-11-05"})
        self.entry("income", "2026-11-08", "Salary", "300")
        self.entry("expense", "2026-11-09", "Groceries", "25")
        self.entry("transfer", "2026-11-13", "Top up savings", "20", destination_id=savings)
        self.entry("investment", "2026-11-14", "Contribution", "40", destination_id=investment)
        self.entry("valuation", "2026-11-15", "Fund loss", "-10", account_id=investment)
        values, details = waterfall(self.db.ledger(), month_window("2026-11", TODAY))
        self.assertEqual([int(row["value"]) for row in values], [100000, 30000, -7500, -1000, 30000, 151500])
        shared = next(item for item in details["expense"] if item.get("shared"))
        self.assertEqual((shared["amount"], shared["cash_delta"]), ("5000", "-10000"))
        snapshot = dashboard(self.db.ledger(), "1M", TODAY)
        self.assertEqual(snapshot["summary"]["wealth"], "151500")
        self.assertEqual(snapshot["shared_balance"], "5000")

    def test_replay_preserves_month_balances_and_descendant_drilldown(self):
        home = self.category("Home")
        groceries = self.category("Groceries", home)
        self.entry("expense", "2026-10-01", "Shop one", "22.15", category_id=groceries)
        self.entry("expense", "2026-11-01", "Shop two", "51.55", category_id=groceries)
        self.entry("expense", "2026-11-02", "Shop three", "8.12", category_id=home)
        ledger = self.db.ledger()
        november = month_window("2026-11", TODAY)
        october = replay(ledger, month_window("2026-10", TODAY))
        current = replay(ledger, november)
        self.assertEqual((october["summary"]["expense"], current["summary"]["expense"]), ("2215", "5967"))
        self.assertEqual((current["through"], october["through"]), (TODAY.isoformat(), "2026-10-31"))
        self.assertEqual(october["scale"], current["scale"])
        result = transaction_list(ledger, page=1, size=25, category=home, window=november, descendants=True)
        self.assertEqual([entry["name"] for entry in result["entries"]], ["Shop three", "Shop two"])
        with self.assertRaises(ApiError):
            month_window("2026-12", TODAY)

    def test_detective_surfaces_three_flags_and_supporting_entries(self):
        category = self.category("Groceries")
        for month in (8, 9, 10):
            self.entry("expense", f"2026-{month:02d}-01", "Usual shop", "70", category_id=category)
        self.entry("expense", "2026-10-26", "Late in month store", "400", category_id=category)
        self.entry("expense", "2026-11-03", "Current shop", "150", category_id=category)
        for month, amount in ((8, "10"), (9, "10"), (10, "10"), (11, "14")):
            self.entry("expense", f"2026-{month:02d}-05", "Music plan", amount)
        for day in (2, 9, 16):
            self.entry("expense", f"2026-11-{day:02d}", "Weekly class", "10")
        ledger = self.db.ledger()
        flags = detective(ledger, month_window("2026-11", TODAY))
        by_kind = {flag["kind"]: flag for flag in flags if flag["label"] != "Uncategorized"}
        self.assertEqual(set(by_kind), {"category", "price", "new"})
        self.assertEqual(by_kind["category"]["baseline"], "7000")
        self.assertEqual(by_kind["price"]["baseline"], "1000")
        self.assertEqual(len(by_kind["new"]["history"]), 2)
        history = handle_insights("/api/insights/detective", {
            "month": "2026-11", "flag": by_kind["price"]["id"], "prior": "1",
        }, ledger, TODAY)
        self.assertEqual(history["total"], 3)
        self.assertTrue(all(item["amount"] == "1000" for item in history["rows"]))

    def test_detector_does_not_infer_a_pattern_from_two_payments(self):
        self.entry("expense", "2026-10-12", "Possible subscription", "10")
        self.entry("expense", "2026-11-12", "Possible subscription", "15")
        self.assertEqual(detective(self.db.ledger(), month_window("2026-11", TODAY)), [])

    def test_shared_price_detection_uses_the_bill_amount_even_when_personal_cost_is_unchanged(self):
        alice = self.shared("people", {"name": "Alice", "tag": "alice"})
        data = {"name": "Internet", "frequency": "monthly", "amount": "100", "first_on": "2026-08-05",
                "account_id": self.cash, "participants": [alice]}
        payment = self.shared("rules", data)
        for month in ("08", "09", "10"):
            day = "2026-" + month + "-05"
            self.shared(f"rules/{payment}/record", {"due_on": day, "posted_on": day})
        self.shared(f"rules/{payment}", dict(data, amount="110", split="custom",
                    shares={"me": "50", str(alice): "60"}), "PUT")
        self.shared(f"rules/{payment}/record", {"due_on": "2026-11-05", "posted_on": "2026-11-05"})
        ledger = self.db.ledger()
        flag = next(row for row in detective(ledger, month_window("2026-11", TODAY)) if row["kind"] == "price")
        self.assertEqual((flag["amount"], flag["baseline"]), ("11000", "10000"))
        self.assertEqual(flag["recent"][0]["amount"], 5000)
        evidence = handle_insights("/api/insights/detective", {"flag": flag["id"]}, ledger, TODAY)
        self.assertEqual(evidence["rows"][0]["amount"], "11000")
