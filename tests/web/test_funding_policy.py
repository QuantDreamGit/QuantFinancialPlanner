"""Persistence and workspace authorization for funding policy changes."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from apps.web.funding_policy import handle_funding_policy, initialize_funding
from apps.web.storage import Database
from apps.web.values import ApiError
from apps.web.workspaces import WorkspaceRegistry, group_id


TODAY = date(2026, 11, 21)
BASE = "/api/funding"


class FundingPolicyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.primary = Database(Path(folder.name) / "funding.sqlite3")
        self.primary.initialize()
        self.registry = WorkspaceRegistry(self.primary)
        self.partner = self.profile("Partner")
        self.outsider = self.profile("Outside profile")
        self.household = self.group("Household", self.partner)

    def profile(self, name):
        return self.registry.save({"kind": "profile", "name": name})["id"]

    def group(self, name, partner):
        return self.registry.save({"kind": "group", "name": name, "members": [1, partner]})["id"]

    def workspace_database(self, scope):
        workspace = self.registry.workspace(scope)
        database = self.registry.database(workspace)
        initialize_funding(database)
        return workspace, database

    def account(self, scope, name, kind="checking"):
        _, database = self.workspace_database(scope)
        return database.save_account({
            "name": name, "kind": kind, "opened_on": "2026-11-01", "opening_balance": "5000",
        }, TODAY)

    def api(self, scope, method, path, data=None, *, actor=1, demo_access=None):
        workspace, database = self.workspace_database(scope)
        handler = SimpleNamespace(
            database=database, workspace=workspace, user={"profile_id": actor, "is_admin": actor == 1},
            read_data=lambda _: {} if data is None else data,
        )
        if demo_access is not None:
            handler.can_use_personal_profile = demo_access
        result, status = handle_funding_policy(handler, BASE + path, method, TODAY)
        self.assertIn(status, (200, 201))
        return result

    def set_policy(self, scope, ident, reserve="0", available=None, **options):
        return self.api(scope, "PUT", f"/accounts/{ident}", {"reserve": reserve, "available_on": available}, **options)

    def account_policy(self, scope, ident, **options):
        return next(account for account in self.api(scope, "GET", "/policy", **options)["accounts"] if account["id"] == ident)

    def route(self, scope, source, destination, days=2, **options):
        return self.api(scope, "POST", "/routes", {
            "source_id": source, "destination_id": destination, "days": days,
        }, **options)["id"]

    def test_owner_reserve_and_lock_follow_the_account_between_its_workspaces(self):
        savings = self.account(self.partner, "Term savings", "savings")
        inherited = group_id(self.partner, savings)
        self.assertFalse(self.account_policy(self.household, inherited)["can_edit"])
        self.assertTrue(self.account_policy(self.household, inherited, actor=self.partner)["can_edit"])
        self.assertEqual(self.account_policy(self.household, inherited)["opened_on"], "2026-11-01")

        with self.assertRaises(ApiError) as error:
            self.set_policy(self.household, inherited, "10")
        self.assertEqual(error.exception.status, 403)
        self.set_policy(self.partner, savings, "875,25", "2027-03-31", actor=self.partner)
        visible = self.account_policy(self.household, inherited)
        self.assertEqual((visible["reserve"], visible["available_on"]), ("87525", "2027-03-31"))
        self.assertEqual(visible["profile_id"], self.partner)
        self.set_policy(self.household, inherited, "950.50", actor=self.partner)
        restored = self.account_policy(self.partner, savings, actor=self.partner)
        self.assertEqual((restored["reserve"], restored["available_on"]), ("95050", None))

    def test_group_routes_require_source_approval_and_do_not_cross_workspaces(self):
        personal = self.account(self.partner, "Partner savings", "savings")
        source = group_id(self.partner, personal)
        payer = self.account(self.household, "Joint checking")
        another = self.group("Another household", self.partner)
        self.account(another, "Other checking")
        with self.assertRaises(ApiError) as error:
            self.route(self.household, source, payer)
        self.assertEqual(error.exception.status, 403)

        ident = self.route(self.household, source, payer, actor=self.partner)
        self.assertEqual(ident, self.route(self.household, source, payer, actor=self.partner))
        with self.assertRaises(ApiError) as conflict:
            self.route(self.household, source, payer, days=14, actor=self.partner)
        self.assertEqual(conflict.exception.status, 409)
        hosted = self.api(self.household, "GET", "/policy")["routes"]
        self.assertEqual(len(hosted), 1)
        self.assertFalse(hosted[0]["can_edit"])
        self.assertEqual((hosted[0]["source_id"], hosted[0]["destination_id"]), (source, payer))
        self.assertEqual(self.api(another, "GET", "/policy")["routes"], [])
        self.assertEqual(self.api(self.partner, "GET", "/policy", actor=self.partner)["routes"], [])

        with self.assertRaises(ApiError) as change:
            self.api(self.household, "PUT", f"/routes/{ident}", {"days": 5})
        self.assertEqual(change.exception.status, 403)
        self.api(self.household, "PUT", f"/routes/{ident}", {"days": 14}, actor=self.partner)
        self.assertEqual(self.api(self.household, "GET", "/policy")["routes"][0]["days"], 14)
        with self.assertRaises(ApiError) as removal:
            self.api(self.household, "DELETE", f"/routes/{ident}")
        self.assertEqual(removal.exception.status, 403)
        self.api(self.household, "DELETE", f"/routes/{ident}", actor=self.partner)
        self.assertEqual(self.api(self.household, "GET", "/policy")["routes"], [])

    def test_group_members_can_configure_joint_accounts_but_outsiders_cannot_read_policies(self):
        joint = self.account(self.household, "Joint account")
        other = self.account(self.household, "Joint savings", "savings")
        self.set_policy(self.household, joint, "900", actor=self.partner)
        self.assertEqual(self.account_policy(self.household, joint)["reserve"], "90000")
        ident = self.route(self.household, joint, other, days=0, actor=self.partner)
        self.api(self.household, "DELETE", f"/routes/{ident}")
        with self.assertRaises(ApiError) as error:
            self.api(self.household, "GET", "/policy", actor=self.outsider)
        self.assertEqual(error.exception.status, 403)

    def test_investments_require_explicit_source_maturity_and_can_never_be_destinations(self):
        bank = self.account(1, "Checking")
        note = self.account(1, "Fixed income", "investment")
        with self.assertRaisesRegex(ApiError, "maturity date"):
            self.route(1, note, bank)
        with self.assertRaisesRegex(ApiError, "destination"):
            self.route(1, bank, note)
        self.set_policy(1, note, "500", "2026-12-11")
        route_id = self.route(1, note, bank, days=14)
        active = self.api(1, "GET", "/policy")["routes"][0]
        self.assertEqual((active["id"], active["days"], active["active"]), (route_id, 14, True))
        self.set_policy(1, note, "500")
        blocked = self.api(1, "GET", "/policy")["routes"][0]
        self.assertEqual((blocked["id"], blocked["active"], blocked["can_edit"]), (route_id, False, True))
        self.api(1, "DELETE", f"/routes/{route_id}")
        self.assertEqual(self.api(1, "GET", "/policy")["routes"], [])

    def test_removed_member_endpoints_do_not_appear_even_if_the_group_snapshot_retains_the_account(self):
        mine = group_id(1, self.account(1, "Mine"))
        theirs = group_id(self.partner, self.account(self.partner, "Theirs"))
        stranger = group_id(self.outsider, self.account(self.outsider, "Outside"))
        ident = self.route(self.household, mine, theirs)
        with self.assertRaises(ApiError) as outside:
            self.route(self.household, mine, stranger)
        self.assertEqual(outside.exception.status, 404)
        self.registry.save({"kind": "group", "name": "Household", "members": [1, self.outsider]}, self.household)

        state = self.api(self.household, "GET", "/policy")
        self.assertEqual(state["routes"], [])
        self.assertNotIn(theirs, [account["id"] for account in state["accounts"]])
        with self.assertRaises(ApiError) as edit:
            self.api(self.household, "PUT", f"/routes/{ident}", {"days": 3})
        self.assertEqual(edit.exception.status, 404)

    def test_authorized_simulated_profiles_can_approve_routes_and_policy(self):
        savings = self.account(self.partner, "Demo partner account", "savings")
        source = group_id(self.partner, savings)
        joint = self.account(self.household, "Joint checking")
        simulated_access = lambda ident: ident == self.partner
        self.set_policy(self.household, source, "125", demo_access=simulated_access)
        self.assertTrue(self.account_policy(self.household, source, demo_access=simulated_access)["can_edit"])
        self.assertEqual(self.account_policy(self.partner, savings, actor=self.partner)["reserve"], "12500")
        self.route(self.household, source, joint, demo_access=simulated_access)

    def test_reserve_lock_and_delay_validation(self):
        source = self.account(1, "Checking")
        target = self.account(1, "Savings", "savings")
        for reserve, when in (("-0.01", None), ("0", "2026-10-31"), ("0", "2026-02-31"), ("0", True)):
            with self.subTest(reserve=reserve, when=when), self.assertRaises(ApiError):
                self.set_policy(1, source, reserve, when)
        for days in (-1, 15, True, 0.5, "1"):
            with self.subTest(days=days), self.assertRaises(ApiError):
                self.route(1, source, target, days)
        with self.assertRaisesRegex(ApiError, "different accounts"):
            self.route(1, source, source)
        self.assertEqual(self.api(1, "GET", "/policy")["routes"], [])
        self.assertIsNone(self.account_policy(1, source)["available_on"])

    def test_reopened_database_retains_policy_and_account_deletion_clears_local_rules(self):
        source = self.account(1, "Savings", "savings")
        target = self.account(1, "Checking")
        self.set_policy(1, source, "150.75")
        self.route(1, source, target)
        self.primary = Database(self.primary.path)
        self.primary.initialize()
        self.registry = WorkspaceRegistry(self.primary)
        self.assertEqual(self.account_policy(1, source)["reserve"], "15075")
        self.assertEqual(len(self.api(1, "GET", "/policy")["routes"]), 1)
        self.primary.delete("accounts", source)
        self.assertEqual(self.api(1, "GET", "/policy")["routes"], [])
        with self.primary.connection(snapshot=True) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM funding_account_policy").fetchone()[0], 0)
