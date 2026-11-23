import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from apps.web.budgets import limit_report
from apps.web.decision import scorecard
from apps.web.demo import sample_ledger
from apps.web.demo_seed import seed_comparisons, seed_family, seed_profile
from apps.web.forecast import add_months, handle_forecast, plan_actual, projected_events
from apps.web.funding import funding_check
from apps.web.goals import report
from apps.web.returns import return_details
from apps.web.storage import Database
from apps.web.values import ApiError
from apps.web.workspaces import WorkspaceRegistry


class SampleSeedTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.location = Path(directory.name)
        self.today = date(2026, 11, 21)

    def database(self, name):
        db = Database(self.location / (name + ".sqlite"))
        db.initialize()
        return db

    def test_personal_loads_real_domain_data_relative_to_reference_date(self):
        database = self.database("profile")
        self.assertTrue(seed_profile(database, self.today, "personal"))
        ledger = database.ledger()
        original = sample_ledger(self.today)
        self.assertEqual({account["name"] for account in ledger.accounts},
                         {account["name"] for account in original.accounts} | {"Term deposit"})
        self.assertGreater(len(ledger.entries), len(original.entries))
        self.assertTrue(all(entry["posted_on"] <= self.today.isoformat() for entry in ledger.entries))
        self.assertSetEqual({goal["name"] for goal in report(database, self.today)["goals"]},
                            {"Emergency cushion", "Summer holiday"})
        deposit = return_details(database, self.today)["returns"][0]
        self.assertEqual((deposit["product"], deposit["mode"], deposit["amount"], deposit["rate"]),
                         ("deposit", "rate", "1500", "0.3"))
        self.assertGreater(deposit["posted_count"], 0)
        events = projected_events(database, self.today, self.today + timedelta(days=65))
        self.assertTrue(any(event["purpose"] == "return" for event in events if "purpose" in event))
        self.assertTrue(any(event.get("scheduled_settlement_id") and event["amount"] > 0 for event in events))
        runs = scorecard(database, self.today)
        self.assertGreater(runs["overall"]["count"], 0)
        self.assertEqual(len(runs["runs"]), 2)

    def test_reopening_keeps_edits_and_deletions(self):
        database = self.database("editable")
        seed_profile(database, self.today, "personal")
        before = database.ledger()
        checking = next(account for account in before.accounts if account["kind"] == "checking")
        database.save_account({"name": "My edited sample account", "kind": "checking",
                               "opened_on": checking["opened_on"], "opening_balance": "300.11"}, self.today, checking["id"])
        with database.connection(write=True) as conn:
            conn.execute("DELETE FROM goals WHERE name = 'Summer holiday'")
        self.assertFalse(seed_profile(Database(database.path), self.today + timedelta(days=40), "personal"))
        after = database.ledger()
        self.assertEqual(len(after.accounts), len(before.accounts))
        self.assertEqual(len(after.entries), len(before.entries))
        self.assertIn("My edited sample account", [account["name"] for account in after.accounts])
        self.assertNotIn("Summer holiday", {goal["name"] for goal in report(database, self.today)["goals"]})

    def test_failed_seed_rolls_back_all_records_and_can_be_retried(self):
        database = self.database("failed")
        with database.connection(write=True) as conn:
            conn.execute("""
                CREATE TRIGGER sample_failure BEFORE INSERT ON goals
                BEGIN SELECT RAISE(ABORT, 'sample test failure'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            seed_profile(database, self.today, "personal")
        self.assertEqual(database.ledger().accounts, [])
        self.assertEqual(database.ledger().entries, [])
        with database.connection(write=True) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='sample_seed'").fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decision_runs").fetchone()[0], 0)
            conn.execute("DROP TRIGGER sample_failure")
        self.assertTrue(seed_profile(database, self.today, "personal"))

    def test_unmarked_existing_real_ledger_is_left_alone(self):
        database = self.database("nonempty")
        database.save_account({"name": "Existing checking", "kind": "checking",
                               "opened_on": "2026-10-01", "opening_balance": "200.00"}, self.today)
        original = database.ledger()
        with self.assertRaisesRegex(ApiError, "already contains local accounts"):
            seed_profile(database, self.today, "personal")
        self.assertEqual(database.ledger(), original)

    def test_saved_examples_have_actual_points_and_keep_edits(self):
        database = self.database("comparisons")

        class TestProjection:
            def compare(self, inputs):
                first = date.fromisoformat(inputs["startingMonth"] + "-01")
                rows = [{"month": add_months(first, offset).isoformat()[:7], "availableCash": "1800000",
                         "earmarkedCash": "0"} for offset in range(5)]
                return dict(inputs), {"plans": [{"approach": "gradual", "projection": rows}]}

        seed_profile(database, self.today, "personal")
        self.assertTrue(seed_comparisons(database, self.today, TestProjection()))
        handler = SimpleNamespace(database=database, server=SimpleNamespace(database=database))
        saved, status = handle_forecast(handler, "/api/forecast/comparisons", {}, "GET", self.today)
        self.assertEqual((status, len(saved)), (200, 1))
        comparison = saved[0]["id"]
        actual, status = handle_forecast(handler, f"/api/forecast/actual-simulation/{comparison}", {}, "GET", self.today)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(sum(point["actual"] is not None for point in actual["result"]["points"]), 2)
        scenarios = database.scenario_list()
        self.assertEqual(len(scenarios), 1)
        scenario = database.scenario(scenarios[0]["id"])
        self.assertTrue(plan_actual(database, scenario, self.today)["has_targets"])
        with database.connection(write=True) as conn:
            conn.execute("UPDATE forecast_comparisons SET name='Edited' WHERE id=?", (comparison,))
            conn.execute("DELETE FROM scenarios")
        self.assertFalse(seed_comparisons(database, self.today, TestProjection()))
        self.assertEqual(database.scenario_list(), [])
        saved, _ = handle_forecast(handler, "/api/forecast/comparisons", {}, "GET", self.today)
        self.assertEqual([row["name"] for row in saved], ["Edited"])

    def test_comparison_can_seed_without_purchase_engine(self):
        database = self.database("no-projection")
        with self.assertRaisesRegex(ApiError, "sample personal workspace"):
            seed_comparisons(database, self.today)

        class Unavailable:
            def compare(self, inputs):
                raise ApiError("Engine unavailable", status=503)

        seed_profile(database, self.today, "personal")
        self.assertTrue(seed_comparisons(database, self.today, Unavailable()))
        self.assertFalse(seed_comparisons(database, self.today))
        self.assertEqual(database.scenario_list(), [])
        with database.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM forecast_comparisons").fetchone()[0], 1)

    def test_provisioned_family_has_mirrors_limits_and_exact_reimbursement_stress(self):
        primary = self.database("registry")
        registry = WorkspaceRegistry(primary)
        demo = registry.provision_demo(1, self.today, None)
        family = registry.workspace(demo["family_id"])
        database = registry.database(family)
        self.assertFalse(seed_family(database, family, self.today))
        self.assertEqual(primary.ledger().accounts, [])
        account = next(item for item in database.ledger().accounts if item["name"] == "Family checking")
        owners = {item["profile_id"] for item in database.ledger().accounts if "profile_id" in item}
        self.assertEqual(owners, {demo["personal_id"], demo["partner_id"]})
        limits = limit_report(database, family, self.today.isoformat()[:7], self.today)
        self.assertEqual({row["name"] for row in limits["limits"]}, {"Family spending", "Shared household expenses"})
        result = funding_check(SimpleNamespace(workspace=family), database, {
            "account_id": account["id"], "amount": "900.00", "days": 60, "reimbursement_delay": 21,
            "purchase_on": (self.today + timedelta(days=10)).isoformat(),
        }, self.today)
        self.assertFalse(result["base"]["safe"])
        self.assertTrue(result["base"]["can_fund"])
        self.assertEqual(result["base"]["first_shortfall"]["amount"], "20000")
        self.assertEqual(result["base"]["moved"], "45000")
        self.assertEqual(result["late"]["moved"], "75000")
        self.assertEqual(result["delayed_reimbursements"][0]["amount"], "30000")

        later = registry.provision_demo(1, self.today + timedelta(days=30), None)
        self.assertEqual(later, demo)
        self.assertEqual(len(database.ledger().accounts), 9)
