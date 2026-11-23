import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from apps.web.forecast import save_rule
from apps.web.goals import (
    allocate, load_goals, prepare_report, redirect_input, report, save_goal, save_redirect,
)
from apps.web.shared import handle_shared
from apps.web.storage import Database
from apps.web.values import ApiError


TODAY = date(2026, 11, 19)


def funding(account, earmarked="0", monthly="0"):
    return {"account_id": account, "earmarked": earmarked, "monthly": monthly}


def by_id(payload):
    return {goal["id"]: goal for goal in payload["goals"]}


class GoalsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "finance.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()

    def account(self, name, balance, kind="savings"):
        return self.db.save_account({
            "name": name, "kind": kind, "opened_on": "2026-01-01", "opening_balance": balance,
        }, TODAY)

    def goal(self, name, target, *links, goal_id=None, target_on=""):
        return save_goal(self.db, {
            "name": name, "target": target, "funding": list(links), "target_on": target_on,
        }, TODAY, goal_id)

    def report(self, *, events=(), redirect=None, reserve=None):
        with self.db.connection(snapshot=True) as conn:
            return prepare_report(self.db.ledger(conn=conn), load_goals(conn), TODAY, events,
                                  redirect=redirect, reserve=reserve)

    def test_multiple_accounts_persist_and_fund_one_goal(self):
        savings = self.account("Holiday savings", "700.25")
        second = self.account("Extra savings", "200")
        goal = self.goal("Holiday", "680.25", funding(savings, "600.25"), funding(second, "80"),
                         target_on="2027-03-01")
        self.db = Database(self.path)
        self.db.initialize()
        result = self.report()
        self.assertEqual(result["accounts"][0]["unassigned"], "10000")
        row = by_id(result)[goal]
        self.assertEqual(row["target_on"], "2027-03-01")
        self.assertEqual(row["estimated_on"], TODAY.isoformat())
        self.assertEqual(row["funded"], "68025")
        self.assertEqual([item["backed"] for item in row["funding"]], ["60025", "8000"])

    def test_edit_checks_actual_capacity_and_rolls_back(self):
        savings = self.account("Savings", "100")
        something_else = self.account("Wallet", "500", "cash")
        trip = self.goal("Trip", "80", funding(savings, "70"))
        with self.assertRaisesRegex(ApiError, "enough unassigned"):
            self.goal("Repairs", "50", funding(savings, "31"))
        with self.assertRaisesRegex(ApiError, "existing savings account"):
            self.goal("Trip updated", "80", funding(something_else, "60"), goal_id=trip)
        with self.assertRaisesRegex(ApiError, "once per goal"):
            self.goal("Repairs", "30", funding(savings, "10"), funding(savings, "20"))
        self.assertEqual(by_id(self.report())[trip]["name"], "Trip")

    def test_concurrent_requests_cannot_double_earmark_existing_cash(self):
        savings = self.account("Shared savings", "100")

        def attempt(name):
            try:
                self.goal(name, "80", funding(savings, "80"))
                return True
            except ApiError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sum(executor.map(attempt, ["Holiday", "Car"])), 1)
        self.assertEqual(self.report()["accounts"][0]["unassigned"], "2000")

    def test_withdrawal_reduces_backing_without_rewriting_earmarks(self):
        savings = self.account("Savings", "100")
        trip = self.goal("Trip", "80", funding(savings, "70"))
        car = self.goal("Car", "80", funding(savings, "30"))
        self.db.save_entry({
            "name": "Hotel", "kind": "expense", "posted_on": "2026-11-01", "amount": "20.01", "account_id": savings,
        }, TODAY)
        goals = by_id(self.report())
        self.assertEqual(goals[trip]["funded"], "5599")
        self.assertEqual(goals[car]["funded"], "2400")
        self.assertEqual(goals[trip]["funding"][0]["earmarked"], "7000")
        self.assertEqual(allocate(1, {5: 5, 2: 5}), {5: 0, 2: 1})
        edited = self.goal("Trip renamed", "80", funding(savings, "70"), goal_id=trip)
        self.assertEqual(edited, trip)
        self.assertEqual(by_id(self.report())[trip]["name"], "Trip renamed")

    def test_future_cash_accounts_for_shared_withdrawal_and_scheduled_deposit(self):
        savings = self.account("Multigoal", "600")
        checking = self.account("Checking", "1000", "checking")
        trip = self.goal("Trip", "400", funding(savings, "300", "100"))
        car = self.goal("Car", "300", funding(savings, "200", "100"))
        events = [
            {"date": date(2026, 12, 10), "account_id": savings, "amount": -20000, "shared": True},
            {"date": date(2026, 12, 20), "account_id": checking, "amount": 50000},
            {"date": date(2027, 1, 12), "account_id": savings, "amount": 30000},
        ]
        result = self.report(events=events)
        self.assertEqual(result["accounts"][0]["first_pressure_on"], "2026-12-10")
        goals = by_id(result)
        self.assertEqual(goals[trip]["estimated_on"], "2027-01-31")
        self.assertEqual(goals[car]["estimated_on"], "2027-01-31")
        self.assertEqual(goals[trip]["funded"], "30000")
        no_deposit = by_id(self.report(events=events[:2]))
        self.assertIsNone(no_deposit[trip]["estimated_on"])
        self.assertEqual(no_deposit[trip]["projected_funded"], "24000")
        self.assertEqual(no_deposit[car]["projected_funded"], "16000")

    def test_redirect_preview_is_read_only_and_apply_conserves_monthly_total(self):
        savings = self.account("Savings", "1000")
        trip = self.goal("Trip", "600", funding(savings, "0", "150"))
        car = self.goal("Car", "400", funding(savings, "0", "50"))
        redirect = redirect_input({
            "from_id": trip, "to_id": car, "account_id": savings, "amount": "100", "expected_from": "150", "expected_to": "50",
        })
        preview = by_id(self.report(redirect=redirect))
        self.assertEqual((preview[trip]["before_on"], preview[trip]["estimated_on"]), ("2027-03-31", "2027-11-30"))
        self.assertEqual((preview[car]["before_on"], preview[car]["estimated_on"]), ("2027-07-31", "2027-02-28"))
        self.assertEqual(by_id(self.report())[trip]["funding"][0]["monthly"], "15000")
        with self.assertRaisesRegex(ApiError, "Preview the redirect"):
            save_redirect(self.db, dict(redirect, expected_from=None))
        save_redirect(self.db, redirect)
        with self.assertRaisesRegex(ApiError, "monthly amounts changed"):
            save_redirect(self.db, redirect)
        actual = by_id(self.report())
        self.assertEqual(sum(int(row["funding"][0]["monthly"]) for row in actual.values()), 20000)
        self.assertEqual(actual[car]["estimated_on"], "2027-02-28")
        self.assertEqual(actual[trip]["funded"], "0")

    def test_pending_and_posted_shared_payment_adjusts_goal_capacity_once(self):
        savings = self.account("Savings", "1000")
        checking = self.account("Checking", "300", "checking")
        goal = self.goal("Holiday", "950", funding(savings, "850", "100"))
        self.assertEqual(by_id(report(self.db, TODAY))[goal]["estimated_on"], "2026-12-31")

        def shared(path, data):
            handler = SimpleNamespace(server=SimpleNamespace(database=self.db), read_data=lambda _: data)
            return handle_shared(handler, "/api/shared/" + path, {}, "POST", TODAY)[0]["id"]

        person = shared("people", {"name": "Alex", "tag": "alex"})
        rule = shared("rules", {
            "name": "Shared rental", "first_on": TODAY.isoformat(), "frequency": "once",
            "account_id": savings, "amount": "200", "participants": [person],
        })
        pending_report = report(self.db, TODAY)
        self.assertEqual(pending_report["accounts"][0]["first_pressure_on"], TODAY.isoformat())
        pending = by_id(pending_report)[goal]
        self.assertEqual(pending["funded"], "85000")
        self.assertIsNone(pending["estimated_on"])
        self.assertEqual(pending["projected_funded"], "80000")

        shared(f"rules/{rule}/record", {"due_on": TODAY.isoformat(), "posted_on": TODAY.isoformat()})
        posted = by_id(report(self.db, TODAY))[goal]
        self.assertEqual(posted["funded"], "80000")
        self.assertEqual(posted["projected_funded"], "80000")

        shared("settlements", {
            "person_id": person, "account_id": savings, "amount": "100", "direction": "receive",
            "posted_on": TODAY.isoformat(),
        })
        save_rule(self.db, {
            "name": "Savings transfer", "start_on": "2027-01-12", "cadence": "once", "kind": "transfer",
            "account_id": checking, "destination_id": savings, "amount": "100",
        })
        settled = by_id(report(self.db, TODAY))[goal]
        self.assertEqual(settled["funded"], "85000")
        self.assertEqual(settled["estimated_on"], "2027-01-31")

    def test_goal_reference_blocks_account_deletion(self):
        savings = self.account("Savings", "1000")
        self.goal("End of year gifts", "600", funding(savings, "0", "150"))
        with self.assertRaises(ApiError) as failure:
            self.db.delete("accounts", savings)
        self.assertEqual(failure.exception.status, 409)

    def test_suggested_monthly_pace_rounds_up_and_protects_competing_goal(self):
        savings = self.account("Savings", "1000")
        goal = self.goal("Holiday", "600", funding(savings, "200", "100"), target_on="2027-02-28")
        self.goal("Kitchen", "600", funding(savings, "0", "100"))
        advice = by_id(self.report())[goal]["recommendation"]
        self.assertEqual(advice["status"], "increased_pace")
        self.assertEqual(advice["installments"], 3)
        self.assertEqual(advice["configured_end"], "50000")
        self.assertEqual(advice["competing_monthly"], "10000")
        self.assertEqual(advice["suggested_monthly"], "13334")
        self.assertEqual([month["links"][0]["amount"] for month in advice["suggested_schedule"]],
                         ["13334", "13334", "13332"])

    def test_refuses_high_unbacked_request_with_competing_allocations(self):
        savings = self.account("Savings", "500")
        goal = self.goal("Holiday", "350", funding(savings, "100", "500"), target_on="2027-02-28")
        self.goal("Kitchen", "300", funding(savings, "100", "200"))
        advice = by_id(self.report())[goal]["recommendation"]
        self.assertEqual(advice["status"], "funding_gap")
        self.assertEqual(advice["additional_needed"], "15000")
        self.assertEqual(advice["first_configured"], "16667")
        self.assertEqual(advice["configured_monthly"], "50000")
        self.assertIsNone(advice["suggested_monthly"])

    def test_late_savings_deposit_and_reserve_use_exact_time_window(self):
        savings = self.account("Savings", "200")
        goal = self.goal("Holiday", "600", funding(savings), target_on="2027-01-31")
        events = [{"date": date(2027, 1, 15), "account_id": savings, "amount": 40000}]
        advice = by_id(self.report(events=events, reserve=10000))[goal]["recommendation"]
        self.assertEqual(advice["status"], "risky")
        self.assertEqual(advice["straight_line"], "30000")
        self.assertEqual(advice["suggested_monthly"], "40000")
        self.assertEqual(advice["suggested_reserve"], {"minimum": "0", "on": "2026-12-31"})
        self.assertEqual(advice["configured_reserve"]["minimum"], "20000")
        self.assertEqual([month["links"][0]["amount"] for month in advice["suggested_schedule"]], ["20000", "40000"])

        self.goal("Holiday", "600", funding(savings), goal_id=goal, target_on="2027-01-10")
        earlier = by_id(self.report(events=events))[goal]["recommendation"]
        self.assertEqual(earlier["installments"], 1)
        self.assertEqual(earlier["status"], "funding_gap")
        self.assertEqual(earlier["additional_needed"], "40000")

    def test_recommendation_handles_funded_missing_past_and_near_deadlines(self):
        savings = self.account("Savings", "100")
        funded = self.goal("Funded", "100", funding(savings, "100"))
        missing = self.goal("Missing", "100", funding(savings))
        passed = self.goal("Past", "100", funding(savings), target_on="2026-11-18")
        upcoming = self.goal("Soon", "100", funding(savings), target_on="2026-11-30")
        distant = self.goal("Distant", "100", funding(savings), target_on="2032-01-31")
        result = by_id(self.report())
        self.assertEqual([result[key]["recommendation"]["status"] for key in (funded, missing, passed, upcoming, distant)],
                         ["funded", "deadline_missing", "deadline_passed", "deadline_too_soon", "outside_horizon"])

    def test_suggestion_avoids_allocating_cash_before_known_drawdown(self):
        stable = self.account("Stable savings", "50")
        volatile = self.account("Variable savings", "50")
        goal = self.goal("Trip", "100", funding(stable), funding(volatile), target_on="2027-02-28")
        events = [
            {"date": date(2027, 1, 10), "account_id": volatile, "amount": -5000},
            {"date": date(2027, 2, 10), "account_id": volatile, "amount": 5000},
        ]
        advice = by_id(self.report(events=events))[goal]["recommendation"]
        self.assertEqual(advice["suggested_monthly"], "5000")
        self.assertEqual(advice["suggested_schedule"], [
            {"on": "2026-12-31", "links": [{"account_id": stable, "amount": "5000"}]},
            {"on": "2027-02-28", "links": [{"account_id": volatile, "amount": "5000"}]},
        ])


if __name__ == "__main__":
    unittest.main()
