"""Exercise account return schedules across the public dashboard API boundaries."""

import base64
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
from apps.web.workspaces import WorkspaceRegistry, group_id


class ReturnIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Database(Path(temporary.name) / "interest.sqlite3")
        self.database.initialize()
        self.registry = WorkspaceRegistry(self.database)
        self.server = SimpleNamespace(database=self.database, workspaces=self.registry, projection=None)
        server_date = patch("apps.web.__main__.date")
        self.addCleanup(server_date.stop)
        self.clock = server_date.start()
        self.clock.today.return_value = date(2026, 11, 20)

    def api(self, method, path, *, data=None, query=None, workspace=1):
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.command = method
        handler.server = self.server
        handler.headers = Message()
        handler.headers["X-Workspace"] = str(workspace)
        if data is not None:
            body = json.dumps(data).encode()
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = str(len(body))
            handler.rfile = BytesIO(body)
        result, status = handler.api(path, query or {})
        self.assertIn(status, (200, 201), (method, path, result))
        return result

    def account(self, name, kind, balance, workspace=1):
        return self.api("POST", "/api/accounts", workspace=workspace, data={
            "name": name, "kind": kind, "opening_balance": balance, "opened_on": "2026-08-01",
        })["id"]

    def schedule(self, name, instrument, *, workspace=1, **fields):
        ident = self.api("POST", "/api/returns", workspace=workspace, data={
            "name": name, "instrument_id": instrument, "mode": "fixed", "amount": "12.50",
            "start_on": "2026-11-25", **fields,
        })["id"]
        listing = self.api("GET", "/api/returns", workspace=workspace)["returns"]
        return next(item for item in listing if item["id"] == ident)

    def day(self, iso_date, workspace=1):
        result = self.api("GET", "/api/forecast/runway", workspace=workspace, query={"month": iso_date[:7]})
        return next(row for row in result["days"] if row["date"] == iso_date)

    def test_note_coupon_paid_into_checking_flows_through_forecast_and_ledger_once(self):
        bank = self.account("Checking", "checking", "1000")
        note = self.account("Fixed income", "investment", "2000")
        coupon = self.schedule("Note coupon", note, payout_account_id=bank, category_id=None)
        self.assertEqual(self.day("2026-11-24")["projected"], "100000")
        scheduled = self.day("2026-11-25")
        item = next(row for row in scheduled["items"] if row["id"] == coupon["commitment_id"])
        self.assertEqual((item["purpose"], item["kind"], item["status"]), ("return", "income", "pending"))
        self.assertEqual(scheduled["projected"], "101250")
        estimate = self.api("POST", "/api/forecast/simulate", data={"months": 2, "reserve": "0"})
        self.assertEqual(estimate["points"][-1]["baseline"], "102500")
        self.assertEqual(self.api("GET", "/api/dashboard")["summary"]["wealth"], "300000")

        self.clock.today.return_value = date(2026, 11, 25)
        command = {"rule_id": coupon["commitment_id"], "date": "2026-11-25"}
        first = self.api("POST", "/api/forecast/occurrences", data=command)
        self.assertEqual(self.api("POST", "/api/forecast/occurrences", data=command), first)
        day = self.day("2026-11-25")
        self.assertEqual((day["actual"], day["projected"]), ("101250", "101250"))
        self.assertEqual(next(row for row in day["items"] if row["id"] == coupon["commitment_id"])["status"], "posted")
        summary = self.api("GET", "/api/dashboard")["summary"]
        self.assertEqual((summary["cash"], summary["investments"], summary["wealth"], summary["income"]),
                         ("101250", "200000", "301250", "1250"))
        self.assertEqual(len(self.database.ledger().entries), 1)

    def test_retained_investment_return_does_not_raise_spendable_cash(self):
        self.account("Checking", "checking", "1000")
        note = self.account("Note account", "investment", "2000")
        retained = self.schedule("Retained coupon", note, amount="12.50")
        pending = self.day("2026-11-25")
        self.assertEqual(pending["projected"], "100000")
        self.clock.today.return_value = date(2026, 11, 25)
        self.api("POST", "/api/forecast/occurrences", data={"rule_id": retained["commitment_id"], "date": "2026-11-25"})
        summary = self.api("GET", "/api/dashboard")["summary"]
        self.assertEqual((summary["cash"], summary["investments"], summary["wealth"], summary["income"]),
                         ("100000", "201250", "301250", "1250"))
        self.assertEqual(self.day("2026-11-25")["projected"], "100000")

    def test_savings_interest_reconciles_with_csv_and_consumes_projected_receipt(self):
        savings = self.account("Savings", "savings", "2500")
        interest = self.schedule("Savings interest", savings, mode="rate", rate="0.125",
                                 principal="2500", start_on="2026-11-20")
        self.assertEqual(interest["amount"], "313")
        self.assertEqual(self.day("2026-11-20")["projected"], "250313")
        csv = b"Date,Description,Amount,Currency,Reference\n2026-11-20,Monthly bank interest,3.13,EUR,I-001\n"
        upload = {"source": "csv", "file": base64.b64encode(csv).decode(), "account_id": savings}
        columns = self.api("POST", "/api/imports/inspect", data=upload)["mapping"]
        row = self.api("POST", "/api/imports/preview", data=dict(upload, mapping=columns))["rows"][0]
        self.assertEqual(row["review_group"], "matches")
        self.assertEqual(row["commitments"][0]["id"], interest["commitment_id"])
        selection = {"rows": [{"token": row["token"], "line": row["line"], "action": "commitment",
                               "rule_id": interest["commitment_id"], "due_on": "2026-11-20",
                               "name": row["name"], "posted_on": row["posted_on"], "amount": "3.13"}]}
        applied = self.api("POST", "/api/imports/commit", data=selection)
        repeated = self.api("POST", "/api/imports/commit", data=selection)
        self.assertEqual((applied["saved"], repeated["already"]), (1, 1))
        day = self.day("2026-11-20")
        self.assertEqual((day["actual"], day["projected"]), ("250313", "250313"))
        self.assertEqual(day["items"][0]["status"], "posted")
        self.assertEqual(self.api("GET", "/api/dashboard")["summary"]["income"], "313")
        self.assertEqual(len(self.database.ledger().entries), 1)

    def test_group_bridge_inherits_member_schedule_and_posts_without_doubling(self):
        partner = self.api("POST", "/api/workspaces", data={"name": "Partner", "kind": "profile"})["id"]
        group = self.api("POST", "/api/workspaces", data={
            "name": "Household", "kind": "group", "members": [1, partner],
        })["id"]
        self.account("Checking", "checking", "400")
        their_account = self.account("Partner savings", "savings", "800", workspace=partner)
        # Keep the return on the member-owned savings account and bridge its identity.
        theirs = self.schedule("Partner deposit", their_account, workspace=partner)
        inherited_rule = group_id(partner, theirs["commitment_id"])
        inherited_account = group_id(partner, their_account)
        mirrored = self.api("GET", "/api/returns", workspace=group)["returns"]
        copied = next(row for row in mirrored if row["commitment_id"] == inherited_rule)
        self.assertEqual(copied["instrument_id"], inherited_account)
        self.assertEqual(self.day("2026-11-25", workspace=group)["projected"], "121250")
        self.assertEqual(self.day("2026-11-25", workspace=1)["projected"], "40000")
        self.assertEqual(self.day("2026-11-25", workspace=partner)["projected"], "81250")

        self.clock.today.return_value = date(2026, 11, 25)
        self.api("POST", "/api/forecast/occurrences", workspace=partner,
                 data={"rule_id": theirs["commitment_id"], "date": "2026-11-25"})
        group_day = self.day("2026-11-25", workspace=group)
        self.assertEqual((group_day["actual"], group_day["projected"]), ("121250", "121250"))
        self.assertEqual(next(row for row in group_day["items"] if row["id"] == inherited_rule)["status"], "posted")
        rows = self.api("GET", "/api/transactions", workspace=group, query={"account": str(inherited_account)})["entries"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], "1250")
        self.assertEqual(self.api("GET", "/api/dashboard", workspace=1)["summary"]["income"], "0")
        self.assertEqual(self.api("GET", "/api/dashboard", workspace=group)["summary"]["income"], "1250")
