import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from apps.web.budgets import end_limit, handle_budgets, initialize_budgets, limit_report, write_limit
from apps.web.forecast import save_rule
from apps.web.shared import _post_rule, _save_person, _save_rule
from apps.web.storage import Database
from apps.web.values import ApiError
from apps.web.workspaces import WorkspaceRegistry, group_id


FAMILY = {
    "kind": "group", "id": 7, "name": "Family",
    "members": [{"id": 11, "name": "Alex"}, {"id": 12, "name": "Jamie"}],
}


class FamilyTestLedger(Database):
    """Add workspace ownership annotations to regular SQLite test data."""

    def __init__(self, path):
        super().__init__(path)
        self.owners = {}

    def ledger(self, conn=None):
        ledger = super().ledger(conn)
        for row in (*ledger.accounts, *ledger.entries):
            key = row.get("account_id", row["id"])
            if key in self.owners:
                row["profile_id"] = self.owners[key]
        return ledger


class BudgetTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = FamilyTestLedger(Path(folder.name) / "family.sqlite")
        self.today = date(2026, 11, 20)
        self.db.initialize()
        initialize_budgets(self.db)
        self.alex = self.account("Alex budget account", 11)
        self.jamie = self.account("Jamie budget account", 12)
        self.home = self.db.save_category({"name": "Household", "kind": "expense", "color": "rose"})
        self.groceries = self.db.save_category(
            {"name": "Groceries", "kind": "expense", "color": "rose", "parent_id": self.home},
        )

    def account(self, name, member):
        account = self.db.save_account(
            {"name": name, "kind": "checking", "opened_on": "2026-09-01", "opening_balance": "1000"}, self.today,
        )
        self.db.owners[account] = member
        return account

    def expense(self, account, amount, **changes):
        return self.db.save_entry({
            "kind": "expense", "posted_on": "2026-11-19", "name": "Store", "account_id": account,
            "amount": amount, "category_id": self.groceries, **changes,
        }, self.today)

    def limit(self, **changes):
        return write_limit(self.db, FAMILY, {
            "name": "Household maximum", "monthly_limit": "100.00", "starts_month": "2026-10",
            "category_id": self.home, **changes,
        })

    def report(self, month="2026-11", **options):
        return limit_report(self.db, FAMILY, month, self.today, **options)["limits"]

    def test_rollup_uses_exact_personal_spending_and_scope_and_never_blocks_actuals(self):
        household = self.limit()
        member = self.limit(name="Alex maximum", monthly_limit="40", member_id=11)
        self.expense(self.alex, "45.01")
        self.expense(self.jamie, "50.00", name="Jamie's groceries")
        self.expense(self.jamie, "5.00", posted_on="2026-10-31")
        self.expense(self.jamie, "900.00", kind="income", category_id=None, name="Salary")
        self.expense(self.jamie, "30.00", category_id=None, name="Out of scope")

        with self.db.connection(write=True) as conn:
            friend = _save_person(conn, {"name": "Pat", "tag": "pat"})
            rule = _save_rule(conn, {
                "name": "Shared groceries", "first_on": "2026-11-20", "frequency": "once",
                "amount": "10.01", "account_id": self.alex, "participants": [friend],
                "category_id": self.groceries,
            }, self.today)
            _post_rule(conn, rule, {"due_on": "2026-11-20", "posted_on": "2026-11-20"}, self.today)

        result = {rule["id"]: rule for rule in self.report()}
        total = result[household]
        self.assertEqual((total["spent"], total["remaining"], total["exceeded"]), ("10002", "-2", True))
        self.assertEqual({row["id"]: row["amount"] for row in total["members"]}, {11: "5002", 12: "5000"})
        self.assertEqual(len(total["entries"]), 3)
        self.assertEqual(result[member]["spent"], "5002")
        self.assertTrue(result[member]["exceeded"])
        self.assertEqual({row["id"]: row["spent"] for row in self.report("2026-10")}[household], "500")

    def test_profile_readonly_has_own_entries_and_applicable_family_totals(self):
        shared = self.limit()
        individual = self.limit(name="Alex maximum", member_id=11)
        self.limit(name="Jamie maximum", member_id=12)
        self.expense(self.alex, "12.05", name="Alex spend")
        self.expense(self.jamie, "15", name="Other spend")
        handler = SimpleNamespace(
            workspace={"kind": "profile", "id": 11}, database=self.db,
            group_budget_contexts=lambda: [(self.db, FAMILY)], read_data=lambda _limit: {},
        )
        result, code = handle_budgets(handler, "/api/budgets", {}, "GET", self.today)
        month = result["groups"][0]
        self.assertEqual(code, 200)
        self.assertEqual(result["mode"], "profile")
        self.assertEqual({rule["id"] for rule in month["limits"]}, {shared, individual})
        total = next(rule for rule in month["limits"] if rule["id"] == shared)
        self.assertEqual((total["spent"], total["own_spent"]), ("2705", "1205"))
        self.assertEqual([item["name"] for item in total["entries"]], ["Alex spend"])
        self.assertEqual(total["members"], [])
        with self.assertRaises(ApiError) as denied:
            handle_budgets(handler, "/api/budgets", {}, "POST", self.today)
        self.assertEqual(denied.exception.status, 403)

    def test_scheduled_expense_warns_and_projected_shared_spending_uses_personal_amount(self):
        self.limit()
        self.expense(self.alex, "90.00")
        save_rule(self.db, {
            "name": "Bill", "kind": "expense", "purpose": "bill", "cadence": "once",
            "amount": "12", "account_id": self.jamie, "category_id": self.home, "start_on": "2026-11-30",
        })
        row = self.report()[0]
        self.assertEqual((row["spent"], row["scheduled"], row["projected"]), ("9000", "1200", "10200"))
        self.assertFalse(row["exceeded"])
        self.assertTrue(row["projected_exceeded"])
        with self.db.connection(write=True) as conn:
            friend = _save_person(conn, {"name": "Pat", "tag": "pat"})
            _save_rule(conn, {
                "name": "Shared cleaning", "first_on": "2026-11-21", "frequency": "once",
                "amount": "16", "account_id": self.jamie, "participants": [friend], "category_id": self.home,
            }, self.today)
        row = self.report()[0]
        self.assertEqual((row["scheduled"], row["projected"], row["projected_exceeded"]), ("2000", "11000", True))

    def test_period_membership_overlap_and_archive(self):
        previous = self.limit(ends_month="2026-10")
        next_limit = self.limit(starts_month="2026-11")
        with self.assertRaisesRegex(ApiError, "already covers"):
            self.limit(starts_month="2026-10", ends_month="2026-12")
        with self.assertRaisesRegex(ApiError, "current member"):
            self.limit(starts_month="2027-01", member_id=1000)
        with self.assertRaisesRegex(ApiError, "last month"):
            self.limit(starts_month="2026-12", ends_month="2026-11")
        end_limit(self.db, FAMILY, next_limit, "2026-11")
        self.assertEqual([row["id"] for row in self.report("2026-10")], [previous])
        self.assertEqual([row["id"] for row in self.report()], [next_limit])
        self.assertEqual(self.report("2026-12"), [])
        with self.assertRaises(ApiError) as foreign:
            end_limit(self.db, dict(FAMILY, id=8), next_limit, "2026-11")
        self.assertEqual(foreign.exception.status, 404)

    def test_reopen_preserves_exact_limit_and_different_groups_do_not_leak(self):
        self.limit(monthly_limit="90071992547409.95")
        reopened = FamilyTestLedger(self.db.path)
        reopened.initialize()
        self.assertEqual(limit_report(reopened, FAMILY, "2026-11", self.today)["limits"][0]["monthly_limit"],
                         "9007199254740995")
        self.assertEqual(limit_report(reopened, dict(FAMILY, id=8), "2026-11", self.today)["limits"], [])

    def test_real_group_tracks_mirrored_category_and_member_spending(self):
        registry = WorkspaceRegistry(self.db)
        partner = registry.save({"name": "Partner", "kind": "profile"})
        workspace = registry.save({"name": "Family", "kind": "group", "members": [1, partner["id"]]})
        other = registry.database(partner)
        bank = other.save_account(
            {"name": "Personal bank", "kind": "checking", "opened_on": "2026-10-01", "opening_balance": "500"},
            self.today,
        )
        category = other.save_category({"name": "Shopping", "kind": "expense", "color": "rose"})
        other.save_entry({
            "name": "Family shopping", "kind": "expense", "account_id": bank, "category_id": category,
            "amount": "35.05", "posted_on": "2026-11-19",
        }, self.today)
        group = registry.database(workspace)
        item_id = write_limit(group, workspace, {
            "name": "Partner spending maximum", "member_id": partner["id"], "monthly_limit": "35",
            "category_id": str(group_id(partner["id"], category)), "starts_month": "2026-11",
        })
        limit = limit_report(group, workspace, "2026-11", self.today)["limits"][0]
        self.assertEqual((item_id, limit["spent"], limit["remaining"]), (limit["id"], "3505", "-5"))
        self.assertTrue(limit["exceeded"])
        self.assertEqual(limit["entries"][0]["member_id"], partner["id"])
        self.assertEqual(limit_report(group, workspace, "2026-11", self.today, only_member=1)["limits"], [])
