import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from apps.web.decision import goal_tradeoff, optimizer, persisted_run, save_run, scorecard
from apps.web.forecast import save_rule, save_simulation
from apps.web.goals import save_goal
from apps.web.risk import Model, analyze, build_model, capacity_curve, deterministic_stress, evaluate
from apps.web.returns import save_return
from apps.web.shared import handle_shared
from apps.web.storage import Database
from apps.web.values import ApiError


TODAY = date(2026, 11, 20)
ZERO_RISK = {"paths": 100, "seed": 19, "days": 45, "reserve": "500", "confidence": 100,
             "purchase_on": "2026-11-22", "purchase_amount": "700", "expense_pct": 0,
             "income_pct": 0, "income_miss_pct": 0, "delay_pct": 0, "shock_pct": 0}


class DecisionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(Path(folder.name) / "decision.sqlite3")
        self.db.initialize()
        self.cash = self.account("Checking", "checking", "1000")

    def account(self, name, kind, balance):
        return self.db.save_account({"name": name, "kind": kind, "opening_balance": balance,
                                     "opened_on": "2026-05-01"}, TODAY)

    def scheduled(self, name, kind, amount, when, **extras):
        return save_rule(self.db, {"name": name, "kind": kind, "start_on": when, "amount": amount,
                                   "account_id": self.cash, "cadence": "once", **extras})

    def test_reverse_capacity_requires_daily_reserve_before_and_after_purchase(self):
        self.scheduled("Payday", "income", "500", "2026-11-25")
        result = analyze(build_model(self.db, ZERO_RISK, TODAY))
        self.assertEqual(result["capacity"], "50000")
        self.assertEqual(result["earliest_purchase"], "2026-11-25")
        self.assertEqual(result["first_breaches"], {"2026-11": 100})
        self.assertEqual(result["stress"]["baseline_breach"], "2026-11-22")
        self.scheduled("Early bill", "expense", "600", "2026-11-21")
        result = analyze(build_model(self.db, ZERO_RISK, TODAY))
        self.assertIsNone(result["capacity"])
        self.assertIsNone(result["earliest_purchase"])
        self.assertEqual(result["stress"]["baseline_breach"], "2026-11-21")

    def test_seeded_common_draws_rank_removed_shock_and_income_effects(self):
        self.scheduled("Income", "income", "300", "2026-11-21")
        inputs = dict(ZERO_RISK, purchase_amount="0", shock_pct=100, shock_amount="1500",
                      income_pct=20, income_miss_pct=10)
        first = analyze(build_model(self.db, inputs, TODAY))
        second = analyze(build_model(self.db, inputs, TODAY))
        self.assertEqual(first, second)
        self.assertEqual(first["safe_paths"], 0)
        driver = next(row for row in first["drivers"] if row["key"] == "shocks")
        self.assertEqual(driver["improvement"], 100)
        self.assertEqual(first["drivers"][0]["key"], "shocks")

    def test_payday_risk_keeps_fixed_monthly_investment_payouts(self):
        note = self.account("Bond", "investment", "1000")
        save_return(self.db, {"name": "Coupon", "mode": "fixed", "amount": "20", "instrument_id": note,
                              "payout_account_id": self.cash, "start_on": "2026-11-21"})
        self.scheduled("Payday", "income", "100", "2026-11-21")
        model = build_model(self.db, dict(ZERO_RISK, income_miss_pct=100), TODAY)
        self.assertEqual(model.base[1], 112000)
        self.assertTrue(all(path[1] == 102000 for path in model.paths))

    def test_reverse_price_meets_the_target_and_one_more_cent_does_not(self):
        self.scheduled("Salary", "income", "300", "2026-11-23")
        self.scheduled("Rent", "expense", "130", "2026-11-25")
        model = build_model(self.db, dict(ZERO_RISK, confidence=80, income_pct=20, expense_pct=20,
                                          shock_pct=17, shock_amount="150"), TODAY)
        curve = capacity_curve(model)
        for day in (0, model.purchase, 4, len(curve) - 1):
            ceiling = curve[day]
            self.assertIsNotNone(ceiling)
            self.assertGreaterEqual(evaluate(model, day, ceiling)["safe_paths"], 80)
            self.assertLess(evaluate(model, day, ceiling + 1)["safe_paths"], 80)

    def test_reverse_stress_shows_first_additional_delay_and_exact_expense(self):
        dates = [TODAY + timedelta(days=index) for index in range(8)]
        expected = {"kind": "settlement", "amount": 20000, "name": "From Alex",
                    "scheduled_settlement_id": 1}
        model = Model(TODAY, dates, 30000, {self.cash}, [(2, expected)],
                      [30000, 30000, 50000, 50000, 50000, 50000, 50000, 50000],
                      [], {}, 25000, 10000, 5, 90, 0, 1)
        result = deterministic_stress(model)
        self.assertEqual(result["extra_expense"], "15001")
        self.assertEqual(result["reimbursement"]["first_failing_delay"], 4)
        self.assertIsNone(result["income_cut_pct"])

    def test_historical_sampling_excludes_known_commitments_and_uses_explicit_opt_in(self):
        self.scheduled("Regular rent", "expense", "777", "2026-11-25", cadence="monthly")
        for month in (8, 9, 10):
            self.db.save_entry({"name": "Regular rent", "kind": "expense", "amount": "777",
                                "account_id": self.cash, "posted_on": f"2026-{month:02d}-25"}, TODAY)
        model = build_model(self.db, dict(ZERO_RISK, history=True), TODAY)
        self.assertGreaterEqual(model.history_months, 3)
        self.assertTrue(all(path == model.base for path in model.paths))
        with self.assertRaises(ApiError):
            build_model(self.db, dict(ZERO_RISK, purchase_on="2027-11-20"), TODAY)

    def test_optimizer_selects_delay_for_feasible_plan_without_changing_ledger(self):
        broker = self.account("Portfolio", "investment", "0")
        self.scheduled("Payday", "income", "500", "2026-11-25")
        self.scheduled("Portfolio top-up", "investment", "150", "2026-11-30", destination_id=broker)
        result = optimizer(self.db, {"assumptions": ZERO_RISK, "delay_days": 16, "investment_flex": "200"}, TODAY)
        self.assertEqual(result["candidates"], 15)
        self.assertTrue(result["qualified"])
        self.assertTrue(all(row["success"] == 100 for row in result["frontier"]))
        self.assertTrue(all(row["purchase_on"] >= "2026-11-25" for row in result["frontier"]))
        self.assertEqual(self.db.ledger().entries, [])

    def test_goal_curve_moves_monthly_allocations_without_saving(self):
        savings = self.account("Savings", "savings", "2000")
        first = save_goal(self.db, {"name": "Car", "target": "600", "funding": [
            {"account_id": savings, "earmarked": "0", "monthly": "200"},
        ]}, TODAY)
        second = save_goal(self.db, {"name": "Trip", "target": "600", "funding": [
            {"account_id": savings, "earmarked": "0", "monthly": "100"},
        ]}, TODAY)
        data = {"from_id": first, "to_id": second, "account_id": savings}
        result = goal_tradeoff(self.db, data, TODAY)
        current, maximum = result["points"][0], result["points"][-1]
        self.assertEqual((current["redirect"], maximum["redirect"]), ("0", "20000"))
        self.assertLess(maximum["to_on"], current["to_on"])
        self.assertIsNone(maximum["from_on"])
        optimized = optimizer(self.db, dict(data, assumptions=dict(ZERO_RISK, purchase_amount="50"),
                                            funding_account=savings, delay_days=0, investment_flex="0"), TODAY)
        self.assertEqual(optimized["goal_names"], ["Car", "Trip"])
        self.assertTrue(any(row["redirect"] != "0" for row in optimized["frontier"]))
        self.assertEqual(goal_tradeoff(self.db, data, TODAY), result)

    def test_scorecard_uses_frozen_account_scope_and_only_completed_days(self):
        record = save_run(self.db, {"name": "Reserve", "assumptions": dict(ZERO_RISK, purchase_amount="0")}, TODAY)
        self.assertEqual(scorecard(self.db, TODAY)["overall"]["count"], 0)
        self.account("Outside old scope", "cash", "100000")
        later = date(2026, 11, 29)
        self.db.save_entry({"name": "Unplanned", "account_id": self.cash, "kind": "expense",
                            "amount": "100", "posted_on": "2026-11-25"}, later)
        actual = scorecard(self.db, later)
        self.assertEqual(actual["overall"], {"count": 2, "inside_band": 1, "banded": 2, "coverage": 50,
                                             "bias": "-5000", "mae": "5000"})
        self.assertEqual(persisted_run(Database(self.db.path), record["id"]), record)

    def test_scorecard_separates_deterministic_errors_from_band_coverage(self):
        save_simulation(self.db, {"name": "Baseline checks", "inputs": {"months": 2}}, TODAY)
        result = scorecard(self.db, date(2026, 12, 1))
        self.assertEqual(result["runs"][0]["source"], "what-if")
        self.assertEqual(result["overall"]["count"], 1)
        self.assertEqual(result["overall"]["mae"], "0")
        self.assertEqual(result["overall"]["banded"], 0)
        self.assertIsNone(result["overall"]["coverage"])

    def test_posted_shared_expense_and_scheduled_repayment_drive_different_dates(self):
        def shared(path, values):
            handler = SimpleNamespace(server=SimpleNamespace(database=self.db), read_data=lambda _: values)
            return handle_shared(handler, "/api/shared/" + path, {}, "POST", TODAY)[0]["id"]

        alex = shared("people", {"name": "Alex", "tag": "alex"})
        shared("rules", {"name": "Travel", "amount": "200", "first_on": TODAY.isoformat(),
                         "participants": [alex], "account_id": self.cash, "record_now": True})
        shared("expected", {"person_id": alex, "account_id": self.cash, "due_on": "2026-11-22",
                            "amount": "100", "direction": "receive"})
        assumptions = dict(ZERO_RISK, reserve="650", purchase_amount="250", purchase_on="2026-11-23",
                           delay_pct=100, delay_days=4)
        model = build_model(self.db, assumptions, TODAY)
        result = analyze(model)
        self.assertEqual(model.opening, 80000)
        self.assertEqual(model.base[:4], [80000, 80000, 90000, 90000])
        self.assertEqual(result["stress"]["reimbursement"]["first_failing_delay"], 2)
        self.assertLess(result["success"], 100)
        self.assertGreater(result["drivers"][0]["improvement"], 0)
