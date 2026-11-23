import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from apps.web.forecast import (
    actual_dates,
    archive_rule,
    calendar_occurrences,
    occurrence_dates,
    plan_actual,
    plan_metadata,
    post_occurrence,
    projected_events,
    runway,
    save_rule,
    save_simulation,
    simulate,
)
from apps.web.shared import _post_rule, _save_person, _save_rule, _schedule_settlement
from apps.web.storage import Database
from apps.web.values import ApiError


class ForecastTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(Path(folder.name) / "forecast.sqlite")
        self.db.initialize()
        self.today = date(2026, 11, 1)
        self.bank = self.db.save_account(
            {"name": "Checking", "kind": "checking", "opened_on": "2026-08-01", "opening_balance": "1500"}, self.today
        )
        self.broker = self.db.save_account(
            {"name": "Fund", "kind": "investment", "opened_on": "2026-08-01", "opening_balance": "0"}, self.today
        )
        self.rent = self.db.save_category({"name": "Rent", "kind": "expense", "color": "rose"})

    def commitment(self, **changes):
        return save_rule(self.db, {
            "name": "Rent", "account_id": self.bank, "amount": "600", "kind": "expense",
            "category_id": self.rent, "purpose": "bill", "cadence": "monthly", "start_on": "2026-11-05",
            **changes,
        })

    def entry(self, amount, when, **changes):
        return self.db.save_entry({
            "name": "Rent", "account_id": self.bank, "category_id": self.rent,
            "kind": "expense", "amount": amount, "posted_on": when, **changes,
        }, self.today)

    def test_calendar_clamps_short_months_and_restores_original_day(self):
        monthly = list(occurrence_dates(date(2024, 1, 31), "monthly", date(2024, 2, 1), date(2024, 4, 30)))
        self.assertEqual(monthly, [date(2024, 2, 29), date(2024, 3, 31), date(2024, 4, 30)])
        yearly = list(occurrence_dates(date(2024, 2, 29), "yearly", date(2027, 1, 1), date(2028, 3, 1)))
        self.assertEqual(yearly, [date(2027, 2, 28), date(2028, 2, 29)])
        weekly = list(occurrence_dates(date(2026, 10, 1), "weekly", date(2026, 10, 13), date(2026, 10, 19)))
        self.assertEqual(weekly, [date(2026, 10, 15)])

    def test_runway_exposes_tight_days_and_does_not_post_future_money(self):
        self.commitment()
        self.commitment(name="Payday", kind="income", purpose="payday", category_id=None,
                        amount="1200", start_on="2026-11-20")
        self.commitment(name="Weekend trip", cadence="once", amount="1200", start_on="2026-11-12")
        result = runway(self.db, self.today, self.today)
        self.assertEqual(result["days"][0]["actual"], "150000")
        self.assertEqual(result["days"][4]["projected"], "90000")
        self.assertEqual(result["days"][11]["projected"], "-30000")
        self.assertEqual(result["days"][29]["projected"], "90000")
        self.assertEqual(self.db.ledger().entries, [])

    def test_matching_and_posting_removes_pending_cash_without_duplicate(self):
        rule = self.commitment(start_on="2026-11-01")
        transaction_id = self.entry("605", "2026-11-01")
        payload = {"rule_id": rule, "date": "2026-11-01", "entry_id": transaction_id}
        post_occurrence(self.db, payload, self.today)
        post_occurrence(self.db, payload, self.today)
        result = runway(self.db, self.today, self.today)
        self.assertEqual(result["days"][0]["actual"], "89500")
        self.assertEqual(result["days"][0]["projected"], "89500")
        self.assertEqual(result["days"][0]["items"][0]["status"], "posted")
        self.assertEqual(len(self.db.ledger().entries), 1)

        previous = self.commitment(start_on="2026-10-30", cadence="once", amount="22.25")
        payload = {"rule_id": previous, "date": "2026-10-30"}
        posted = post_occurrence(self.db, payload, self.today)
        self.assertEqual(post_occurrence(self.db, payload, self.today), posted)
        self.assertEqual(len(self.db.ledger().entries), 2)
        self.assertEqual(runway(self.db, self.today, self.today)["days"][0]["projected"], "87275")

    def test_skipping_and_archiving_preserve_posted_occurrences(self):
        rule = self.commitment(start_on="2026-10-05")
        original = {"rule_id": rule, "date": "2026-10-05"}
        transaction_id = post_occurrence(self.db, original, self.today)
        pending = {"rule_id": rule, "date": "2026-11-05"}
        with self.assertRaisesRegex(ApiError, "scheduled day"):
            post_occurrence(self.db, pending, self.today)
        post_occurrence(self.db, dict(pending, skip=True), self.today)
        self.assertEqual(projected_events(self.db, self.today, date(2026, 11, 30)), [])
        post_occurrence(self.db, dict(pending, restore=True), self.today)
        self.assertEqual(len(projected_events(self.db, self.today, date(2026, 11, 30))), 1)
        archive_rule(self.db, rule, self.today)
        self.assertEqual(projected_events(self.db, self.today, date(2026, 11, 30)), [])
        old = calendar_occurrences(self.db, date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual((old[0]["entry_id"], old[0]["status"]), (transaction_id, "posted"))

    def test_scenario_variants_and_reserve_check_use_daily_cash(self):
        rule = self.commitment()
        self.commitment(name="Investment", kind="investment", amount="200", category_id=None,
                        destination_id=self.broker, start_on="2026-11-08")
        self.commitment(name="Payday", kind="income", purpose="payday", category_id=None,
                        amount="1500", start_on="2026-11-22")
        inputs = {"months": 3, "reserve": "500", "purchase_amount": "500", "purchase_on": "2026-11-10",
                  "purchase_shift": 1, "rent_rule": rule, "rent_percent": 50, "investment_delta": "100"}
        result = simulate(self.db, inputs, self.today)
        self.assertEqual(result["baseline_breach"], "2026-11-10")
        self.assertEqual(result["alternative_breach"], "2026-11-08")
        self.assertEqual(result["alternative_purchase_on"], "2026-12-10")
        self.assertEqual((result["points"][1]["baseline"], result["points"][1]["alternative"]), ("170000", "180000"))
        # A modeled reduction cannot release more than the cash otherwise invested.
        reduction = simulate(self.db, dict(inputs, investment_delta="-500"), self.today)
        self.assertEqual(reduction["points"][1]["alternative"], "210000")
        self.assertIsNone(reduction["alternative_breach"])
        saved = save_simulation(self.db, {"name": "Lease sensitivity", "inputs": inputs}, self.today)
        self.entry("90", "2026-11-01")
        with self.db.connection() as conn:
            persisted = conn.execute("SELECT result FROM forecast_comparisons WHERE id = ?", (saved["id"],)).fetchone()
        self.assertEqual(json.loads(persisted["result"]), result)
        self.assertNotEqual(simulate(self.db, inputs, self.today)["points"][0], result["points"][0])

    def test_plan_actual_preserves_scope_and_original_assumptions(self):
        scope = plan_metadata(self.db, {"varianceTargets": [{"category_id": self.rent, "amount": "300"}]})
        self.db.save_account(
            {"name": "Unrelated", "kind": "savings", "opened_on": "2026-08-01", "opening_balance": "9999"}, self.today
        )
        self.entry("50", "2026-09-05")
        self.entry("325", "2026-10-05")
        base = {"name": "Car", "input": scope, "result": {"plans": [
            {"approach": "gradual", "projection": [
                {"month": month, "availableCash": cash, "earmarkedCash": "5000"}
                for month, cash in [("2026-09", "140000"), ("2026-10", "110000"), ("2026-11", "90000")]
            ]}
        ]}}
        result = plan_actual(self.db, base, self.today)
        self.assertEqual([point["actual"] for point in result["points"]], ["145000", "112500", None])
        october = result["categories"]["2026-10"][0]
        self.assertEqual((october["planned"], october["actual"], october["variance"]), ("30000", "32500", "-2500"))
        self.assertEqual(result["points"][1]["plan"], "115000")
        no_target = plan_actual(self.db, dict(base, input={"accountScope": scope["accountScope"]}), self.today)
        self.assertIsNone(no_target["categories"]["2026-10"][0]["variance"])
        self.assertFalse(no_target["has_targets"])
        points = [{"day": "2026-11-01", "opening": True}, {"day": "2026-11-30", "completed_only": True}]
        self.assertEqual([p["actual"] for p in actual_dates(self.db.ledger(), points, {self.bank}, self.today)], [None, None])

    def test_shared_uses_gross_cash_in_runway_and_personal_cost_in_plan_actual(self):
        with self.db.connection(write=True) as conn:
            person = _save_person(conn, {"name": "Riley", "tag": "riley"})
            payment = _save_rule(conn, {
                "name": "Shared rent", "amount": "900", "payer_id": None, "account_id": self.bank,
                "category_id": self.rent, "participants": [person], "frequency": "monthly",
                "first_on": "2026-11-01",
            }, self.today)
        result = simulate(self.db, {"months": 1, "reserve": "700",
                                   "rent_rule": f"shared:{payment}", "rent_percent": 50}, self.today)
        self.assertEqual(result["points"][-1]["baseline"], "60000")
        self.assertEqual(result["points"][-1]["alternative"], "15000")
        self.assertEqual(result["baseline_breach"], "2026-11-01")
        with self.db.connection(write=True) as conn:
            _post_rule(conn, payment, {"due_on": "2026-11-01", "posted_on": "2026-11-01"}, self.today)
            _schedule_settlement(conn, {"direction": "receive", "person_id": person,
                                        "account_id": self.bank, "due_on": "2026-11-10", "amount": "450"}, self.today)
        result = runway(self.db, self.today, self.today)
        self.assertEqual(result["days"][0]["actual"], "60000")
        self.assertEqual(result["days"][0]["projected"], "60000")
        self.assertEqual(result["days"][9]["projected"], "105000")
        self.assertEqual(result["days"][9]["shared"][0]["kind"], "settlement")
        base = {"name": "Car", "input": plan_metadata(self.db, {"varianceTargets": [
            {"category_id": self.rent, "amount": "400"},
        ]}), "result": {"plans": [{"approach": "gradual", "projection": [
            {"month": month, "availableCash": "60000", "earmarkedCash": "0"}
            for month in ("2026-10", "2026-11")
        ]}]}}
        november = plan_actual(self.db, base, date(2026, 12, 1))["categories"]["2026-11"][0]
        self.assertEqual((november["actual"], november["variance"]), ("45000", "-5000"))
