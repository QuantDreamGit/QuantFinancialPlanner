import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from apps.web.auth import ADDRESS_RATE_LIMIT, AuthStore, INVITE_SECONDS, NAME_RATE_LIMIT, RATE_WINDOW, SESSION_SECONDS
from apps.web.storage import Database
from apps.web.values import ApiError
from apps.web.workspaces import WorkspaceRegistry


SETUP = "a one-time operator setup phrase 874972"
ADMIN_PASSWORD = "monochrome postcard habitat"
MEMBER_PASSWORD = "train tickets lavender shutters"
REMOTE = "192.0.2.10"


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        db = Database(Path(folder.name) / "ledger.sqlite3")
        db.initialize()
        self.registry = WorkspaceRegistry(db)
        self.timestamp = 1_800_000_000
        self.auth = AuthStore(self.registry, bootstrap_token=SETUP, clock=lambda: self.timestamp)

    def owner(self):
        return self.auth.bootstrap("  Household.Owner ", ADMIN_PASSWORD, SETUP)

    def profile(self, name="Jamie"):
        return self.registry.save({"name": name, "kind": "profile"})

    def invite(self, actor, profile=None):
        profile = profile or self.profile()
        return self.auth.issue_invite(actor["user"]["id"], profile["id"])

    def assert_status(self, code, action):
        with self.assertRaises(ApiError) as failure:
            action()
        self.assertEqual(failure.exception.status, code)

    def test_bootstrap_requires_setup_secret_and_claims_only_once(self):
        self.assertTrue(self.auth.needs_setup())
        self.assert_status(403, lambda: self.auth.bootstrap("household.owner", ADMIN_PASSWORD, "wrong"))
        self.assert_status(422, lambda: self.auth.bootstrap("household.owner", "", SETUP))
        owner = self.owner()
        self.assertFalse(self.auth.needs_setup())
        self.assertEqual(owner["user"], {"id": 1, "profile_id": 1, "username": "household.owner",
                                         "is_admin": True, "enabled": True})
        self.assertEqual(self.auth.session(owner["token"]), owner["user"])
        self.assert_status(409, lambda: self.auth.bootstrap("second.owner", ADMIN_PASSWORD, SETUP))

    def test_credentials_use_unique_salts_and_session_bearers_are_hashed(self):
        owner = self.owner()
        invite = self.invite(owner)
        member = self.auth.accept_invite(invite["token"], "jamie", ADMIN_PASSWORD, REMOTE)
        with self.registry.connection() as conn:
            passwords = [row[0] for row in conn.execute("SELECT password_hash FROM auth_users ORDER BY id")]
            stored_invite = conn.execute("SELECT token_hash FROM auth_invitations").fetchone()[0]
            tokens = {row[0] for row in conn.execute("SELECT token_hash FROM auth_sessions")}
        salts = {row.split("$")[1] for row in passwords}
        self.assertEqual(len(salts), 2)
        self.assertNotEqual(passwords[0], passwords[1])
        self.assertTrue(all(value.startswith("scrypt-v1$") and ADMIN_PASSWORD not in value for value in passwords))
        self.assertEqual(stored_invite, hashlib.sha256(invite["token"].encode()).digest())
        self.assertEqual(tokens, {hashlib.sha256(record["token"].encode()).digest() for record in (owner, member)})

    def test_invitation_profile_binding_replacement_expiry_and_one_time_use(self):
        owner = self.owner()
        profile = self.profile()
        other = self.profile("Tess")
        first = self.invite(owner, profile)
        replacement = self.invite(owner, profile)
        expiring = self.invite(owner, other)
        self.assert_status(403, lambda: self.auth.accept_invite(first["token"], "jamie", MEMBER_PASSWORD, REMOTE))
        member = self.auth.accept_invite(replacement["token"], "jamie", MEMBER_PASSWORD, REMOTE)
        self.assertEqual(member["user"]["profile_id"], profile["id"])
        self.assertFalse(member["user"]["is_admin"])
        self.assert_status(403, lambda: self.auth.accept_invite(replacement["token"], "different", MEMBER_PASSWORD, REMOTE))
        self.assert_status(403, lambda: self.auth.members(member["user"]["id"]))
        self.assert_status(409, lambda: self.auth.issue_invite(owner["user"]["id"], profile["id"]))
        self.timestamp += INVITE_SECONDS
        self.assert_status(403, lambda: self.auth.accept_invite(expiring["token"], "tess", MEMBER_PASSWORD, REMOTE))
        statuses = {item["id"]: item["status"] for item in self.auth.members(owner["user"]["id"])["invitations"]}
        self.assertEqual(statuses, {first["id"]: "revoked", replacement["id"]: "used", expiring["id"]: "expired"})

    def test_concurrent_redemption_results_in_one_account(self):
        owner = self.owner()
        invite = self.invite(owner)

        def accept(name):
            try:
                return self.auth.accept_invite(invite["token"], name, MEMBER_PASSWORD, REMOTE)
            except ApiError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(accept, ["jamie.one", "jamie.two"]))
        self.assertEqual([result for result in results if isinstance(result, int)], [403])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        members = self.auth.members(owner["user"]["id"])
        self.assertEqual(len(members["users"]), 2)
        self.assertEqual(members["invitations"][0]["status"], "used")

    def test_disable_revoke_and_expire_sessions_without_reactivation(self):
        owner = self.owner()
        invite = self.invite(owner)
        member = self.auth.accept_invite(invite["token"], "jamie", MEMBER_PASSWORD, REMOTE)
        admin_id, member_id = owner["user"]["id"], member["user"]["id"]
        duplicate = self.auth.login("Jamie", MEMBER_PASSWORD, REMOTE)
        self.assertEqual(duplicate["user"], member["user"])
        self.assert_status(409, lambda: self.auth.set_enabled(admin_id, admin_id, False))
        self.assert_status(403, lambda: self.auth.revoke_sessions(member_id, admin_id))

        self.auth.logout(member["token"])
        self.assertIsNone(self.auth.session(member["token"]))
        self.assertIsNotNone(self.auth.session(duplicate["token"]))
        self.auth.set_enabled(admin_id, member_id, False)
        self.assertIsNone(self.auth.session(duplicate["token"]))
        self.assert_status(401, lambda: self.auth.login("jamie", MEMBER_PASSWORD, REMOTE))
        self.auth.set_enabled(admin_id, member_id, True)
        self.assertIsNone(self.auth.session(duplicate["token"]))
        reactivated = self.auth.login("jamie", MEMBER_PASSWORD, REMOTE)
        self.auth.revoke_sessions(admin_id, member_id)
        self.assertIsNone(self.auth.session(reactivated["token"]))
        self.timestamp += SESSION_SECONDS
        self.assertIsNone(self.auth.session(owner["token"]))

    def test_password_change_revokes_old_sessions_and_persists(self):
        owner = self.owner()
        duplicate = self.auth.login("household.owner", ADMIN_PASSWORD, REMOTE)
        user_id = owner["user"]["id"]
        replacement = "fresh password umbrellas everywhere"
        self.assert_status(401, lambda: self.auth.change_password(user_id, "incorrect", replacement, REMOTE))
        changed = self.auth.change_password(user_id, ADMIN_PASSWORD, replacement, REMOTE)
        restarted = AuthStore(self.registry, bootstrap_token=SETUP, clock=lambda: self.timestamp)
        self.assertIsNone(restarted.session(owner["token"]))
        self.assertIsNone(restarted.session(duplicate["token"]))
        self.assertEqual(restarted.session(changed["token"]), owner["user"])
        self.assert_status(401, lambda: restarted.login("household.owner", ADMIN_PASSWORD, REMOTE))
        self.assertEqual(restarted.login("household.owner", replacement, REMOTE)["user"], owner["user"])

    def test_persisted_rate_limits_canonical_names_and_network_prefixes(self):
        self.owner()
        for index in range(NAME_RATE_LIMIT):
            self.assert_status(401, lambda: self.auth.login("missing.user", "wrong", f"192.0.2.{20 + index}"))
        restart = AuthStore(self.registry, bootstrap_token=SETUP, clock=lambda: self.timestamp)
        self.assert_status(429, lambda: restart.login("Missing.User", "wrong", "198.51.100.20"))

        for index in range(ADDRESS_RATE_LIMIT):
            self.assert_status(401, lambda: restart.login(" ", None, f"2001:db8::1:{index}"))
        self.assert_status(429, lambda: restart.login("another.user", "wrong", "2001:db8::dead"))
        self.timestamp += RATE_WINDOW
        self.assert_status(401, lambda: restart.login("missing.user", "wrong", "2001:db8::dead"))

    def test_operator_reset_rotates_without_issuing_a_session(self):
        owner = self.owner()
        replacement = "local reset recovers this account"
        self.assertEqual(self.auth.reset_password("Household.Owner", replacement), owner["user"])
        self.assertIsNone(self.auth.session(owner["token"]))
        with self.registry.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 0)
        self.assert_status(401, lambda: self.auth.login("household.owner", ADMIN_PASSWORD, REMOTE))
        self.assertEqual(self.auth.login("household.owner", replacement, REMOTE)["user"], owner["user"])
        self.assert_status(404, lambda: self.auth.reset_password("unknown.user", replacement))

    def test_wrong_shapes_generic_errors_and_unclaimed_profiles(self):
        owner = self.owner()
        profile = self.profile()
        group = self.registry.save({"kind": "group", "name": "Family", "members": [1, profile["id"]]})
        self.assert_status(422, lambda: self.auth.issue_invite(owner["user"]["id"], group["id"]))
        for token in (None, {"token": "x"}, "☼", "a" * 1000):
            self.assertIsNone(self.auth.session(token))
            self.auth.logout(token)
        for password in ([], "", "\ud800", "🚁" * 300):
            self.assert_status(401, lambda: self.auth.login("household.owner", password, REMOTE))
        invite = self.invite(owner, profile)
        self.auth.revoke_invite(owner["user"]["id"], invite["id"])
        self.assert_status(403, lambda: self.auth.accept_invite(invite["token"], "jamie", MEMBER_PASSWORD, REMOTE))
        self.assertTrue(self.auth.session(owner["token"])["enabled"])
