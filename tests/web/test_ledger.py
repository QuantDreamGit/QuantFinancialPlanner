import tempfile
import unittest
from datetime import date
from pathlib import Path

from apps.web.demo import sample_ledger
from apps.web.reports import dashboard, planner_inputs, selected_window, transaction_list
from apps.web.storage import Database
from apps.web.values import ApiError


TODAY = date(2026, 11, 19)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "test.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()

    def account(self, name, kind, balance, day="2026-05-01"):
        return self.db.save_account({
            "name": name, "kind": kind, "opening_balance": balance, "opened_on": day,
        }, TODAY)

    def category(self, name, kind="expense", parent=None, item_id=None):
        return self.db.save_category({
            "name": name, "kind": kind, "parent_id": parent, "color": "indigo",
        }, item_id)

    def entry(self, account, kind, amount, posted, **fields):
        return self.db.save_entry({
            "kind": kind, "account_id": account, "name": kind, "amount": amount,
            "posted_on": posted, **fields,
        }, TODAY)

    def test_history_reconciles_balances_and_flows(self):
        checking = self.account("Checking", "checking", "1000")
        savings = self.account("Savings", "savings", "500")
        broker = self.account("Broker", "investment", "400", "2026-07-01")
        self.entry(checking, "income", "2000", "2026-06-08")
        self.entry(checking, "expense", "400", "2026-06-20")
        self.entry(checking, "transfer", "300", "2026-07-03", destination_id=savings)
        self.entry(checking, "investment", "100", "2026-07-05", destination_id=broker)
        self.entry(broker, "valuation", "-20", "2026-09-10")
        self.entry(checking, "expense", "10,01", "2026-11-19")

        data = dashboard(self.db.ledger(), "6M", TODAY)
        self.assertEqual(data["summary"], {
            "wealth": "346999", "cash": "218999", "savings": "80000", "investments": "48000",
            "income": "200000", "expense": "41001", "investment": "10000",
        })
        june, july, august, september, october, november = data["points"]
        self.assertEqual(june["wealth"], "310000")
        self.assertEqual(july["wealth"], "350000")
        self.assertEqual(august["wealth"], july["wealth"])
        self.assertEqual(september["wealth"], "348000")
        self.assertEqual(october["expense"], "0")
        self.assertEqual(november["to"], TODAY.isoformat())
        self.assertEqual(november["expense"], "1001")
        self.assertEqual(data["flow_counts"], {"income": 1, "expense": 2, "investment": 1})
        self.assertEqual(planner_inputs(self.db.ledger(), TODAY), {
            "startingMonth": "2026-11", "startingCash": "2989.99", "monthlyNetCashFlow": "0.00",
        })

    def test_cash_withdrawal_and_expense_only_charge_spending_once(self):
        bank = self.account("Card account", "checking", "100")
        cash = self.account("Cash wallet", "cash", "50")
        withdrawal = self.entry(bank, "transfer", "40", "2026-11-10", destination_id=cash)
        purchase = self.entry(cash, "expense", "12", "2026-11-11")
        manual = self.entry(bank, "expense", "5", "2026-11-12", statement_expected=False)
        ledger = self.db.ledger()
        expected = {entry["id"]: entry["statement_expected"] for entry in ledger.entries}
        self.assertEqual(expected, {withdrawal: True, purchase: False, manual: False})
        result = dashboard(ledger, "ALL", TODAY)
        self.assertEqual(result["summary"]["expense"], "1700")
        self.assertEqual({account["name"]: account["balance"] for account in result["accounts"]},
                         {"Card account": "5500", "Cash wallet": "7800"})

    def test_statement_expectation_is_persisted_and_legacy_defaults_use_the_account(self):
        bank = self.account("Debit card", "checking", "100")
        payload = {"name": "Important expense", "kind": "expense", "account_id": bank,
                   "amount": "12", "posted_on": "2026-11-10", "request_key": "statement-test"}
        transaction = self.db.save_entry(payload, TODAY)
        self.assertIs(self.db.ledger().entries[0]["statement_expected"], True)
        with self.assertRaisesRegex(ApiError, "already saved"):
            self.db.save_entry(dict(payload, statement_expected=False), TODAY)
        self.db.save_entry(dict(payload, statement_expected=False), TODAY, transaction)
        self.db.save_entry(dict(payload, name="Better description"), TODAY, transaction)
        self.assertIs(self.db.ledger().entries[0]["statement_expected"], False)
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE entries SET statement_expected=NULL WHERE id=?", (transaction,))
        self.assertIs(self.db.ledger().entries[0]["statement_expected"], True)

    def test_category_rollup_and_direct_entries(self):
        cash = self.account("Checking", "checking", "500")
        parent = self.category("Home")
        child = self.category("Groceries", parent=parent)
        self.entry(cash, "expense", "81.19", "2026-11-05", category_id=parent)
        self.entry(cash, "expense", "12.06", "2026-11-08", category_id=child, labels=["Weekly", "weekly", "Home"])
        self.entry(cash, "expense", "1", "2026-11-09")
        self.entry(cash, "expense", "30", "2026-06-08", category_id=child)

        ledger = self.db.ledger()
        result = dashboard(ledger, "1M", TODAY)
        categories = {item["id"]: item for item in result["categories"]}
        self.assertEqual(categories[parent]["amount"], "9325")
        self.assertEqual(categories[parent]["direct_amount"], "8119")
        self.assertEqual(categories[parent]["count"], 2)
        self.assertEqual(categories[child]["amount"], "1206")
        self.assertEqual(result["summary"]["expense"], "9425")
        self.assertEqual(result["unassigned"]["expense"], {"amount": "100", "count": 1})
        self.assertEqual(result["labels"], ["Weekly", "Home"])
        entries = transaction_list(
            ledger, page=1, size=2, search="weekly", category=child,
            window=selected_window(ledger, "1M", TODAY),
        )
        self.assertEqual(entries["total"], 1)
        self.assertEqual(entries["entries"][0]["amount"], "1206")

    def test_restart_reuses_the_account_category_and_labels(self):
        account = self.account("Savings", "savings", "500")
        category = self.category("Home")
        args = {"category_id": category, "labels": ["recurring"], "request_key": "same-request"}
        first = self.entry(account, "expense", "25", "2026-11-09", **args)
        self.assertEqual(self.entry(account, "expense", "25", "2026-11-09", **args), first)
        with self.assertRaises(ApiError) as conflicting_retry:
            self.entry(account, "expense", "26", "2026-11-09", **args)
        self.assertEqual(conflicting_retry.exception.status, 409)
        restarted = Database(self.path)
        restarted.initialize()
        persisted = dashboard(restarted.ledger(), "ALL", TODAY)
        self.assertEqual(persisted["entry_count"], 1)
        self.assertEqual(persisted["summary"]["savings"], "47500")
        self.assertEqual(persisted["defaults"]["expense"], {
            "account_id": account, "destination_id": None, "category_id": category, "labels": ["recurring"],
        })

    def test_invalid_moves_have_no_ledger_side_effect(self):
        checking = self.account("Checking", "checking", "100")
        savings = self.account("Savings", "savings", "200", "2026-11-01")
        parent = self.category("Home")
        child = self.category("Groceries", parent=parent)
        income = self.category("Salary", kind="income")
        with self.assertRaisesRegex(ApiError, "outside this category"):
            self.category("Home", parent=child, item_id=parent)
        with self.assertRaisesRegex(ApiError, "same money flow"):
            self.category("Home", parent=income, item_id=parent)
        with self.assertRaisesRegex(ApiError, "both account opening dates"):
            self.entry(checking, "transfer", "20", "2026-10-21", destination_id=savings)
        with self.assertRaisesRegex(ApiError, "investment destination"):
            self.entry(checking, "investment", "20", "2026-11-02", destination_id=savings)
        with self.assertRaisesRegex(ApiError, "different destination"):
            self.entry(checking, "transfer", "20", "2026-11-02", destination_id=checking)
        self.assertEqual(self.db.ledger().entries, [])
        self.assertEqual(dashboard(self.db.ledger(), "6M", TODAY)["summary"]["wealth"], "30000")
        with self.assertRaises(ApiError) as in_use:
            self.db.delete("categories", parent)
        self.assertEqual(in_use.exception.status, 409)

    def test_unrecorded_history_and_sample_data(self):
        self.assertIsNone(dashboard(self.db.ledger(), "1Y", TODAY)["summary"]["wealth"])
        sample = dashboard(sample_ledger(TODAY), "6M", TODAY)
        self.assertGreater(len(sample["accounts"]), 0)
        self.assertEqual(self.db.ledger().entries, [])
        self.account("Savings", "savings", "150", "2026-11-05")
        result = dashboard(self.db.ledger(), "1Y", TODAY)
        self.assertIsNone(result["points"][0]["wealth"])
        self.assertEqual(result["points"][-1]["wealth"], "15000")

    def test_edits_and_deletes_rebuild_balances_and_defaults(self):
        cash = self.account("Wallet", "cash", "100")
        entry = self.entry(cash, "expense", "10", "2026-11-09")
        self.db.save_entry({
            "kind": "expense", "name": "Corrected expense", "account_id": cash, "amount": "20",
            "posted_on": "2026-11-09",
        }, TODAY, entry)
        self.assertEqual(dashboard(self.db.ledger(), "1M", TODAY)["summary"]["cash"], "8000")
        with self.assertRaises(ApiError):
            self.db.delete("accounts", cash)
        self.db.delete("entries", entry)
        self.assertEqual(dashboard(self.db.ledger(), "1M", TODAY)["summary"]["cash"], "10000")
        self.assertEqual(dashboard(self.db.ledger(), "1M", TODAY)["defaults"], {})
