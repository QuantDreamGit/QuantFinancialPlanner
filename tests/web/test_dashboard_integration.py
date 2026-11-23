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

from apps.web.__main__ import ASSETS, DashboardHandler
from apps.web.storage import Database, SCHEMA
from apps.web.values import ApiError


TODAY = date(2026, 11, 19)
LEGACY_INPUT = {
    "startingMonth": "2026-10", "startingCash": "960.00", "monthlyNetCashFlow": "20.00",
    "minimumReserve": "0.00", "purchasePrice": "100.00", "dueMonth": "2026-12",
}
LEGACY_RESULT = {"reserve": "0", "shortfall": "0", "plans": [
    {"approach": "upfront", "lowestAvailableCash": "86000", "projection": [
        {"month": "2026-10", "availableCash": "86000", "earmarkedCash": "10000"},
        {"month": "2026-11", "availableCash": "88000", "earmarkedCash": "10000"},
        {"month": "2026-12", "availableCash": "90000", "earmarkedCash": "0"},
    ]},
]}


class DashboardIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Database(Path(temp.name) / "legacy.sqlite3")
        with self.database.connection() as conn:
            conn.executescript(SCHEMA)
            conn.execute("""
                INSERT INTO accounts (id, name, kind, opened_on, opening_balance)
                VALUES (1, 'Original savings', 'savings', '2026-09-01', 100000)
            """)
            conn.execute("INSERT INTO categories (id, name, kind, color) VALUES (1, 'Groceries', 'expense', 'mint')")
            conn.execute("""
                INSERT INTO entries (id, kind, name, posted_on, amount, account_id, category_id, labels)
                VALUES (1, 'expense', 'Original groceries', '2026-10-05', 4000, 1, 1, '["food"]')
            """)
            conn.execute("INSERT INTO scenarios (id, name, input, result) VALUES (1, 'Original plan', ?, ?)",
                         (json.dumps(LEGACY_INPUT), json.dumps(LEGACY_RESULT)))
        self.database.initialize()
        frozen_date = patch("apps.web.__main__.date")
        self.addCleanup(frozen_date.stop)
        frozen_date.start().today.return_value = TODAY

    def request(self, method, path, data=None):
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.command = method
        handler.path = path
        handler.server = SimpleNamespace(database=self.database, projection=None, hosts={"127.0.0.1:8080"})
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8080"
        if data is not None:
            body = json.dumps(data).encode()
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = str(len(body))
            handler.rfile = BytesIO(body)
        return handler

    def api(self, method, path, data=None, *, query=None, status=200):
        payload, actual_status = self.request(method, path, data).api(path, query or {})
        self.assertEqual(actual_status, status, (method, path, payload))
        return payload

    def test_existing_ledger_and_saved_projection_survive_reinitialization(self):
        self.database.initialize()
        dashboard = self.api("GET", "/api/dashboard", query={"period": "ALL"})
        self.assertEqual(dashboard["summary"]["wealth"], "96000")
        self.assertEqual(dashboard["accounts"][0]["name"], "Original savings")
        self.assertEqual(dashboard["categories"][0]["name"], "Groceries")
        self.assertEqual(dashboard["recent"][0]["labels"], ["food"])
        legacy = self.api("GET", "/api/scenarios/1")
        self.assertEqual((legacy["input"], legacy["result"]), (LEGACY_INPUT, LEGACY_RESULT))
        comparison = self.api("GET", "/api/forecast/actual/1")
        self.assertEqual([point["actual"] for point in comparison["points"]], ["96000", None, None])
        self.assertEqual([point["plan"] for point in comparison["points"]], ["96000", "98000", "90000"])
        self.assertFalse(comparison["has_targets"])

    def test_shared_activity_flows_through_goals_forecast_and_insights(self):
        goal = self.api("POST", "/api/goals", {
            "name": "Emergency fund", "target": "900",
            "funding": [{"account_id": 1, "earmarked": "800", "monthly": "100"}],
        }, status=201)
        person = self.api("POST", "/api/shared/people", {"name": "Alex", "tag": "alex"}, status=201)
        shared = self.api("POST", "/api/shared/rules", {
            "name": "Shared bill", "account_id": 1, "category_id": 1,
            "frequency": "once", "first_on": TODAY.isoformat(), "amount": "200",
            "participants": [person["id"]],
        }, status=201)
        runway = self.api("GET", "/api/forecast/runway")
        current = next(day for day in runway["days"] if day["date"] == TODAY.isoformat())
        self.assertEqual((current["actual"], current["projected"]), ("96000", "76000"))
        self.assertEqual(current["shared"][0]["name"], "Shared bill")
        goals = self.api("GET", "/api/goals")
        self.assertEqual(goals["goals"][0]["id"], goal["id"])
        self.assertEqual(goals["goals"][0]["funded"], "80000")
        self.assertEqual(goals["accounts"][0]["first_pressure_on"], TODAY.isoformat())

        saved = self.api("POST", "/api/forecast/comparisons", {
            "name": "Try a purchase", "inputs": {
                "months": 2, "reserve": "600", "purchase_amount": "200", "purchase_on": "2026-11-23",
            },
        }, status=201)
        self.assertEqual(saved["result"]["alternative_breach"], "2026-11-23")
        self.assertEqual(self.api("GET", "/api/forecast/comparisons/" + str(saved["id"]))["result"], saved["result"])

        self.api("POST", f"/api/shared/rules/{shared['id']}/record", {
            "due_on": TODAY.isoformat(), "posted_on": TODAY.isoformat(),
        })
        replay = self.api("GET", "/api/insights/replay")
        self.assertEqual(replay["summary"]["expense"], "10000")
        steps = self.api("GET", "/api/insights/waterfall")["steps"]
        values = {step["key"]: step["value"] for step in steps}
        self.assertEqual((values["start"], values["expense"], values["end"]), ("96000", "-10000", "86000"))
        drill = self.api("GET", "/api/insights/waterfall", query={"step": "expense"})
        self.assertEqual(drill["rows"][0]["name"], "Shared bill")
        self.assertIn("@alex", drill["rows"][0]["labels"])
        detective = self.api("GET", "/api/insights/detective")
        self.assertIsInstance(detective["flags"], list)
        goals = self.api("GET", "/api/goals")
        self.assertEqual(goals["goals"][0]["funded"], "76000")
        self.assertIsNone(goals["accounts"][0]["first_pressure_on"])
        self.assertEqual(self.api("GET", "/api/shared")["people"][0]["tag"], "alex")

    def test_preview_insights_use_sample_ledger_only(self):
        actual = self.api("GET", "/api/insights/replay", query={"month": "2026-10"})
        preview = self.api("GET", "/api/insights/replay", query={"month": "2026-10", "sample": "1"})
        self.assertEqual(actual["summary"]["expense"], "4000")
        self.assertGreater(int(preview["summary"]["expense"]), 4000)
        self.assertEqual(self.database.ledger().entries[0]["name"], "Original groceries")

    def test_decision_api_saves_a_seeded_forecast_with_original_account_scope(self):
        setup = self.api("GET", "/api/decision/setup")
        self.assertEqual(setup["accounts"][0]["name"], "Original savings")
        assumptions = {"days": 30, "paths": 100, "seed": 2, "reserve": "500", "shock_pct": 0,
                       "purchase_on": "2026-11-20", "purchase_amount": "200"}
        initial = self.api("POST", "/api/decision/analyze", assumptions)
        self.assertEqual(initial["capacity"], "46000")
        saved = self.api("POST", "/api/decision/runs", {"name": "Holiday risk", "assumptions": assumptions}, status=201)
        self.assertEqual(saved["result"], initial)
        original = self.api("GET", "/api/decision/runs/" + str(saved["id"]))
        self.assertEqual(original["result"]["account_scope"], [1])
        metrics = self.api("GET", "/api/decision/scorecard")
        self.assertTrue(any(row["source"] == "risk" for row in metrics["runs"]))

    def test_reviewed_import_updates_risk_opening_balance_exactly_once(self):
        raw = b"Date,Description,Amount,Currency,Reference\n2026-11-18,Imported bill,-21.35,EUR,X714\n"
        upload = {"source": "csv", "file": base64.b64encode(raw).decode(), "account_id": 1}
        inspect = self.api("POST", "/api/imports/inspect", upload)
        row = self.api("POST", "/api/imports/preview", dict(upload, mapping=inspect["mapping"]))["rows"][0]
        selection = {"rows": [{"token": row["token"], "line": row["line"], "action": "create",
                               "name": row["name"], "posted_on": row["posted_on"], "amount": "-21.35"}]}
        added = self.api("POST", "/api/imports/commit", selection, status=201)
        retry = self.api("POST", "/api/imports/commit", selection, status=201)
        self.assertEqual((added["saved"], retry["already"]), (1, 1))
        assumptions = {"days": 30, "paths": 100, "reserve": "500", "shock_pct": 0}
        risk = self.api("POST", "/api/decision/analyze", assumptions)
        self.assertEqual((risk["opening"], risk["capacity"]), ("93865", "43865"))

    def test_external_payment_before_first_cash_account_still_counts_in_history(self):
        person = self.api("POST", "/api/shared/people", {"name": "Alex", "tag": "alex"}, status=201)
        self.api("POST", "/api/shared/rules", {
            "name": "Older bill", "payer_id": person["id"], "category_id": 1,
            "frequency": "once", "first_on": "2026-08-20", "amount": "100",
            "participants": [person["id"]], "record_now": True,
        }, status=201)
        data = self.api("GET", "/api/dashboard", query={"period": "ALL"})
        self.assertEqual(data["from"], "2026-08-20")
        self.assertEqual(data["summary"]["expense"], "9000")
        self.assertEqual(data["shared_balance"], "-5000")
        replay = self.api("GET", "/api/insights/replay", query={"month": "2026-08"})
        self.assertEqual(replay["summary"]["expense"], "5000")

    def test_transaction_type_correction_reopens_the_matching_commitment(self):
        scheduled = self.api("POST", "/api/forecast/commitments", {
            "name": "Standing charge", "purpose": "bill", "kind": "expense", "amount": "40",
            "account_id": 1, "category_id": 1, "cadence": "monthly", "start_on": "2026-10-05",
        }, status=201)
        with self.assertRaises(ApiError) as error:
            self.api("PUT", "/api/accounts/1", {
                "name": "Original savings", "opened_on": "2026-10-06", "opening_balance": "1000",
            })
        self.assertEqual(error.exception.field, "opened_on")
        self.api("POST", "/api/forecast/occurrences", {
            "rule_id": scheduled["id"], "date": "2026-10-05", "entry_id": 1,
        })
        self.api("PUT", "/api/transactions/1", {
            "kind": "income", "name": "Cash back", "posted_on": "2026-10-05", "account_id": 1, "amount": "40",
        })
        calendar = self.api("GET", "/api/forecast/runway", query={"month": "2026-10"})
        self.assertEqual(calendar["days"][4]["items"][0]["status"], "pending")

    def test_feature_assets_are_served_by_registered_routes(self):
        modules = ("forecast", "insights", "goals", "shared", "decision", "imports", "budgets", "returns", "access",
                   "funding", "documentation")
        assets = {"/": "text/html", "/auth": "text/html", "/dashboard.js": "text/javascript",
                  "/auth.js": "text/javascript", "/auth-shell.js": "text/javascript", "/auth.css": "text/css",
                  "/documentation-topics.js": "text/javascript", "/feature-help.js": "text/javascript",
                  "/attachments.js": "text/javascript", "/attachments.css": "text/css", "/feature-help.css": "text/css"}
        for module in modules:
            assets[f"/{module}.js"] = "text/javascript"
            assets[f"/{module}.css"] = "text/css"

        def dispatched(path):
            handler = self.request("GET", path)
            replies = []
            handler.reply = lambda *reply: replies.append(reply)
            handler.dispatch()
            self.assertEqual(len(replies), 1)
            return replies[0]

        for path, mime in assets.items():
            with self.subTest(path=path):
                self.assertEqual(ASSETS[path][1], mime)
                body, status, content_type = dispatched(path)
                self.assertTrue(body.strip())
                self.assertEqual((status, content_type), (200, mime))
        html = dispatched("/")[0].decode()
        main_js = dispatched("/dashboard.js")[0].decode()
        self.assertIn('id="workspace-switcher"', html)
        self.assertIn('data-style="bar" aria-label="Vertical bars" title="Vertical bars" aria-pressed="true"', html)
        self.assertIn('id="flow-reset"', html)
        self.assertIn('id="flow-fullscreen"', html)
        self.assertIn('id="view-returns"', html)
        self.assertIn('id="auth-shell"', html)
        self.assertIn('id="view-documentation"', html)
        self.assertIn('data-transaction-attachments', html)
        self.assertIn('id="add-starter-categories"', html)
        self.assertIn('id="planner-check-funding"', html)
        for module in modules:
            self.assertIn(f'href="/{module}.css"', html)
            self.assertIn(f'"./{module}.js"', main_js)


if __name__ == "__main__":
    unittest.main()
