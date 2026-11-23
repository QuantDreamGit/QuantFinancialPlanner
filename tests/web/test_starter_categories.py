import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from apps.web.auth import AuthStore
from apps.web.starter_categories import STARTER_CATEGORIES, apply_starter_categories, starter_requested
from apps.web.storage import Database
from apps.web.values import ApiError
from apps.web.workspaces import WorkspaceRegistry


class StarterCategoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Database(Path(folder.name) / "finance.sqlite3")
        self.database.initialize()

    def save_expense(self, name, color, parent_id=None):
        return self.database.save_category({"name": name, "kind": "expense", "color": color, "parent_id": parent_id})

    def test_merges_by_tree_and_preserves_existing_case_ids_and_colors(self):
        home = self.save_expense("hOME", "indigo")
        rent = self.save_expense("RENT", "sky", home)
        other_rent = self.save_expense("Rent", "mint")
        custom = self.save_expense("City tax", "slate", home)
        expected = len(STARTER_CATEGORIES) - 2
        self.assertEqual(apply_starter_categories(self.database), {"added": expected})
        merged = {row["id"]: row for row in self.database.ledger().categories}
        self.assertEqual(merged[home], dict(id=home, kind="expense", name="hOME", color="indigo", parent_id=None))
        self.assertEqual(merged[rent], dict(id=rent, kind="expense", name="RENT", color="sky", parent_id=home))
        self.assertEqual(merged[other_rent]["parent_id"], None)
        self.assertEqual(merged[custom]["parent_id"], home)
        self.assertEqual(next(row["parent_id"] for row in merged.values() if row["name"] == "Utilities"), home)
        self.assertEqual(apply_starter_categories(self.database), {"added": 0})
        self.assertEqual(merged, {row["id"]: row for row in self.database.ledger().categories})

    def test_boolean_opt_in_requires_an_explicit_json_boolean(self):
        self.assertFalse(starter_requested({}))
        self.assertFalse(starter_requested({"starter_categories": False}))
        self.assertTrue(starter_requested({"starter_categories": True}))
        for value in (None, 1, 0, "true", "false", [], {}):
            with self.subTest(value=value), self.assertRaises(ApiError) as error:
                starter_requested({"starter_categories": value})
            self.assertEqual(error.exception.field, "starter_categories")

    def test_failed_seed_rolls_back_every_new_branch(self):
        existing = self.save_expense("Local activity", "rose")
        with self.database.connection(write=True) as connection:
            connection.execute("""
                CREATE TRIGGER reject_category BEFORE INSERT ON categories WHEN NEW.name = 'Index funds'
                BEGIN SELECT RAISE(ABORT, 'forced test failure'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            apply_starter_categories(self.database)
        self.assertEqual([row["id"] for row in self.database.ledger().categories], [existing])

    def test_concurrent_opt_ins_apply_the_preset_once(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(apply_starter_categories, (self.database, self.database)))
        self.assertEqual(sorted(result["added"] for result in results), [0, len(STARTER_CATEGORIES)])
        self.assertEqual(len(self.database.ledger().categories), len(STARTER_CATEGORIES))

    def test_workspace_creation_can_retry_after_seeding_failure(self):
        registry = WorkspaceRegistry(self.database)
        with patch("apps.web.workspaces.apply_starter_categories", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                registry.save({"name": "Failed", "kind": "profile", "starter_categories": True})
        self.assertEqual([workspace["name"] for workspace in registry.list()], ["Personal"])
        member = registry.save({"name": "Partner", "kind": "profile"})
        self.assertEqual(registry.database(member).ledger().categories, [])
        group = registry.save({
            "name": "Family", "kind": "group", "members": [1, member["id"]], "starter_categories": True,
        })
        self.assertEqual(len(registry.database(group).ledger().categories), len(STARTER_CATEGORIES))
        self.assertEqual(registry.database(member).ledger().categories, [])

    def test_failed_seeding_keeps_setup_code_and_invitation_usable(self):
        registry = WorkspaceRegistry(self.database)
        code = "temporary-test-setup-token-0123456789"
        auth = AuthStore(registry, bootstrap_token=code)
        with patch("apps.web.auth.apply_starter_categories", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                auth.bootstrap("owner", "password", code, starter_categories=True)
        self.assertTrue(auth.needs_setup())
        owner = auth.bootstrap("owner", "password", code)
        profile = registry.save({"name": "Partner", "kind": "profile"})
        invitation = auth.issue_invite(owner["user"]["id"], profile["id"])
        credentials = (invitation["token"], "partner", "password", "127.0.0.1")
        with patch("apps.web.auth.apply_starter_categories", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                auth.accept_invite(*credentials, starter_categories=True)
        self.assertEqual(auth.accept_invite(*credentials, starter_categories=True)["user"]["profile_id"], profile["id"])
        self.assertEqual(len(registry.database(profile).ledger().categories), len(STARTER_CATEGORIES))
        self.assertEqual(self.database.ledger().categories, [])
