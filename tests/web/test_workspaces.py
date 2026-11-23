import json
import tempfile
import unittest
from datetime import date
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.web.__main__ import DashboardHandler
from apps.web.storage import Database
from apps.web.workspaces import WorkspaceRegistry, group_id, local_id
from apps.web.values import ApiError


TODAY = date(2026, 11, 20)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.path = Path(work.name) / "finance.sqlite3"
        self.primary = Database(self.path)
        self.primary.initialize()
        self.registry = WorkspaceRegistry(self.primary)
        self.server = SimpleNamespace(database=self.primary, workspaces=self.registry, projection=None)
        frozen = patch("apps.web.__main__.date")
        self.addCleanup(frozen.stop)
        frozen.start().today.return_value = TODAY
        self.person = self.api(1, "POST", "/api/workspaces", {"name": "Partner", "kind": "profile"})["id"]
        self.group = self.api(1, "POST", "/api/workspaces", {
            "name": "Family", "kind": "group", "members": [1, self.person],
        })["id"]

    def api(self, scope, method, route, data=None, **query):
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.server = self.server
        handler.command = method
        handler.headers = Message()
        if scope is not None:
            handler.headers["X-Workspace"] = str(scope)
        if data is not None:
            raw = json.dumps(data).encode()
            handler.rfile = BytesIO(raw)
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = str(len(raw))
        return handler.api(route, query)[0]

    def account(self, scope, balance, kind="checking", name="Bank"):
        return self.api(scope, "POST", "/api/accounts", {
            "name": name, "kind": kind, "opened_on": "2026-01-01", "opening_balance": balance,
        })["id"]

    def expense(self, scope, account, amount, category=None):
        return self.api(scope, "POST", "/api/transactions", {
            "name": "Groceries", "posted_on": "2026-11-20", "kind": "expense", "amount": amount,
            "account_id": account, "category_id": category,
        })["id"]

    def test_personal_balances_private_to_workspace_and_family_has_collision_free_union(self):
        a = self.account(1, "1500")
        b = self.account(self.person, "1000")
        inv = self.account(self.person, "777", kind="investment", name="Investments")
        self.assertEqual(a, b)
        self.expense(1, a, "15")
        self.expense(self.person, b, "60")
        first = self.api(1, "GET", "/api/dashboard")
        second = self.api(self.person, "GET", "/api/dashboard")
        group = self.api(self.group, "GET", "/api/dashboard")
        self.assertEqual(first["summary"]["wealth"], "148500")
        self.assertEqual(second["summary"]["wealth"], "171700")
        self.assertEqual(group["summary"]["wealth"], "320200")
        self.assertEqual(group["summary"]["investments"], "77700")
        self.assertEqual(group["summary"]["expense"], "7500")
        self.assertEqual({account["id"] for account in group["accounts"]}, {
            group_id(1, a), group_id(self.person, b), group_id(self.person, inv),
        })
        self.assertEqual(len({account["name"] for account in group["accounts"]}), 3)
        self.assertEqual({row["profile_id"] for row in group["recent"]}, {1, self.person})

    def test_group_recording_routes_to_owner_and_joint_account_stays_separate(self):
        account = self.account(self.person, "1000")
        category = self.api(self.person, "POST", "/api/categories", {
            "name": "Food", "color": "rose", "kind": "expense",
        })["id"]
        self.assertEqual(self.account(self.group, "500", name="Joint account"), 1)
        inherited = group_id(self.person, account)
        record = self.expense(self.group, inherited, "45.19", group_id(self.person, category))
        self.assertEqual(local_id(record), 1)
        partner_rows = self.api(self.person, "GET", "/api/transactions")["entries"]
        self.assertEqual([(item["amount"], item["category_id"]) for item in partner_rows], [("4519", category)])
        family_rows = self.api(self.group, "GET", "/api/transactions", account=str(inherited))["entries"]
        self.assertEqual([item["id"] for item in family_rows], [record])
        self.assertEqual(self.api(1, "GET", "/api/transactions")["total"], 0)
        self.assertEqual(self.api(self.group, "GET", "/api/dashboard")["summary"]["wealth"], "145481")
        with self.assertRaises(ApiError):
            self.api(self.group, "DELETE", f"/api/accounts/{inherited}")

    def test_group_goals_can_use_inherited_savings_without_affecting_personal_goals(self):
        account = self.account(self.person, "1000", kind="savings")
        group_account = group_id(self.person, account)
        self.api(self.group, "POST", "/api/goals", {
            "name": "Family holiday", "target": "850", "target_on": "2027-03-31",
            "funding": [{"account_id": group_account, "earmarked": "500", "monthly": "50"}],
        })
        report = self.api(self.group, "GET", "/api/goals")
        self.assertEqual(report["goals"][0]["funded"], "50000")
        self.assertIn("recommendation", report["goals"][0])
        self.assertEqual(self.api(self.person, "GET", "/api/goals")["goals"], [])

    def test_personal_goal_earmarks_reduce_group_allocation_capacity(self):
        savings = self.account(self.person, "1000", kind="savings")
        personal = self.api(self.person, "POST", "/api/goals", {
            "name": "Personal reserve", "target": "800",
            "funding": [{"account_id": savings, "earmarked": "700", "monthly": "0"}],
        })["id"]
        group_account = group_id(self.person, savings)
        request = {"name": "Family holiday", "target": "850",
                   "funding": [{"account_id": group_account, "earmarked": "350", "monthly": "50"}]}
        with self.assertRaisesRegex(ApiError, "unassigned"):
            self.api(self.group, "POST", "/api/goals", request)
        request["funding"][0]["earmarked"] = "250"
        family = self.api(self.group, "POST", "/api/goals", request)["id"]
        report = self.api(self.group, "GET", "/api/goals")
        self.assertEqual({goal["id"] for goal in report["goals"]}, {family, group_id(self.person, personal)})
        self.assertEqual(report["accounts"][0]["unassigned"], "5000")
        third = self.api(1, "POST", "/api/workspaces", {"name": "Other", "kind": "profile"})["id"]
        with self.assertRaisesRegex(ApiError, "Move the group goal"):
            self.api(1, "PUT", f"/api/workspaces/{self.group}", {
                "name": "Family", "kind": "group", "members": [1, third],
            })

    def test_group_runway_includes_inherited_ordinary_and_shared_commitments(self):
        first = self.account(1, "1000")
        second = self.account(self.person, "200")
        self.api(1, "POST", "/api/forecast/commitments", {
            "name": "Payday", "kind": "income", "purpose": "payday", "amount": "400",
            "account_id": first, "cadence": "once", "start_on": "2026-11-21",
        })
        contact = self.api(self.person, "POST", "/api/shared/people", {"name": "Alex", "tag": "alex"})["id"]
        self.api(self.person, "POST", "/api/shared/rules", {
            "name": "Shared utility", "frequency": "weekly", "first_on": "2026-11-22", "amount": "100",
            "account_id": second, "participants": [contact],
        })
        runway = self.api(self.group, "GET", "/api/forecast/runway")
        self.assertEqual([row["projected"] for row in runway["days"][19:22]], ["120000", "160000", "150000"])
        self.assertEqual(runway["days"][21]["shared"][0]["personal_amount"], "5000")
        self.assertEqual(runway["days"][20]["items"][0]["profile_id"], 1)
        self.assertEqual(self.api(self.group, "GET", "/api/attention")["bills"][0]["name"], "Shared utility")

    def test_group_limit_alerts_surface_in_both_workspaces_without_merging_personal_balances(self):
        mine = self.account(1, "1000")
        theirs = self.account(self.person, "400")
        self.expense(1, mine, "25")
        self.expense(self.person, theirs, "25")
        self.api(self.group, "POST", "/api/budgets", {
            "name": "Household shopping", "monthly_limit": "40", "starts_month": "2026-11",
        })
        for scope in (1, self.person, self.group):
            alert = self.api(scope, "GET", "/api/attention")["limits"][0]
            self.assertEqual((alert["group"], alert["gap"], alert["exceeded"]), ("Family", "1000", True))
        self.assertEqual(self.api(1, "GET", "/api/dashboard")["summary"]["expense"], "2500")

    def test_membership_removal_refreshes_history_and_preserves_source(self):
        original = self.account(self.person, "300")
        self.expense(self.person, original, "100")
        third = self.api(1, "POST", "/api/workspaces", {"name": "Other", "kind": "profile"})["id"]
        self.assertEqual(self.api(self.group, "GET", "/api/dashboard")["summary"]["wealth"], "20000")
        self.api(1, "PUT", f"/api/workspaces/{self.group}", {
            "name": "Family renamed", "kind": "group", "members": [1, third],
        })
        self.assertIsNone(self.api(self.group, "GET", "/api/dashboard")["summary"]["wealth"])
        self.assertEqual(self.api(self.person, "GET", "/api/dashboard")["summary"]["wealth"], "20000")
        reopened = WorkspaceRegistry(Database(self.path))
        self.assertEqual(reopened.workspace(self.group)["name"], "Family renamed")
        self.assertEqual(self.api(None, "GET", "/api/workspaces")["active"]["id"], 1)
