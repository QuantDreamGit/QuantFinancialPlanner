import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from apps.web.auth import AuthStore
from apps.web.provider_keys import GroqKeyStore, OpenAIKeyStore, _load_fernet
from apps.web.storage import Database
from apps.web.values import ApiError
from apps.web.workspaces import WorkspaceRegistry


SETUP = "test-only provider setup token 8293929"
PASSWORD = "long test passphrase 498142303"
ALPHA_KEY = "gsk_unit-test-first-payments-secret"
BETA_KEY = "gsk_unit-test-other-payments-secret"
OPENAI_ALPHA_KEY = "sk-unit-test-first-openai-secret"
OPENAI_BETA_KEY = "sk-unit-test-other-openai-secret"
OPENAI_REPLACEMENT_KEY = "sk-unit-test-new-openai-secret"


class KeysFixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        database = Database(self.folder / "ledger.sqlite")
        database.initialize()
        self.registry = WorkspaceRegistry(database)
        self.auth = AuthStore(self.registry, SETUP)
        self.alpha = self.auth.bootstrap("alpha", PASSWORD, SETUP)["user"]["id"]
        profile = self.registry.save({"name": "Partner", "kind": "profile"})
        invite = self.auth.issue_invite(self.alpha, profile["id"])
        self.beta = self.auth.accept_invite(invite["token"], "beta", PASSWORD, "192.0.2.1")["user"]["id"]
        self.store = GroqKeyStore(self.registry)

    def assert_unavailable(self, action):
        with self.assertRaises(ApiError) as problem:
            action()
        self.assertEqual(problem.exception.status, 503)
        self.assertNotIn(ALPHA_KEY, str(problem.exception))
        self.assertNotIn(BETA_KEY, str(problem.exception))
        self.assertNotIn(OPENAI_ALPHA_KEY, str(problem.exception))
        self.assertNotIn(OPENAI_BETA_KEY, str(problem.exception))


class OptionalKeyStoreTests(KeysFixture):
    def test_missing_crypto_keeps_other_features_and_avoids_plaintext_persistence(self):
        with patch("apps.web.provider_keys._load_fernet", return_value=None):
            store = GroqKeyStore(self.registry)
        self.assertFalse(store.available)
        self.assertFalse(store.has(self.alpha))
        self.assertIsNone(store.get(self.alpha))
        self.assert_unavailable(lambda: store.save(self.alpha, ALPHA_KEY))
        self.assertFalse(store.key_path.exists())
        self.assertIsNone(store.remove(self.alpha))
        self.assertFalse(store.has(self.alpha))

    def test_openai_stays_optional_without_writing_plaintext(self):
        with patch("apps.web.provider_keys._load_fernet", return_value=None):
            store = OpenAIKeyStore(self.registry)
        self.assertFalse(store.available)
        self.assertIsNone(store.get(self.alpha))
        self.assert_unavailable(lambda: store.save(self.alpha, OPENAI_ALPHA_KEY))
        self.assertFalse(store.key_path.exists())
        self.assertFalse(store.has(self.alpha))


@unittest.skipUnless(_load_fernet() is not None, "requires the optional cryptography package")
class EncryptedKeyStoreTests(KeysFixture):
    def test_round_trip_per_account_and_separate_master_permissions(self):
        self.assertTrue(self.store.available)
        self.assertFalse(self.store.key_path.exists())
        self.assertIsNone(self.store.get(self.alpha))
        self.assertTrue(self.store.save(self.alpha, ALPHA_KEY))
        self.assertIsNone(self.store.get(self.beta))
        self.assertTrue(self.store.save(self.beta, BETA_KEY))
        self.assertEqual(stat.S_IMODE(self.store.key_path.stat().st_mode), 0o600)
        self.assertEqual(self.store.key_path.parent, self.registry.folder)
        self.assertTrue(self.store.has(self.alpha))
        self.assertTrue(self.store.has(self.beta))

        restarted = GroqKeyStore(self.registry)
        self.assertEqual(restarted.get(self.alpha), ALPHA_KEY)
        self.assertEqual(restarted.get(self.beta), BETA_KEY)
        self.assertNotIn(ALPHA_KEY.encode(), self.registry.path.read_bytes())
        self.assertNotIn(BETA_KEY.encode(), self.registry.path.read_bytes())
        self.assertNotIn(self.store.key_path.read_bytes(), self.registry.path.read_bytes())
        with self.registry.connection() as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT typeof(ciphertext) FROM auth_groq_keys")], ["blob", "blob"])

        restarted.remove(self.alpha)
        self.assertIsNone(self.store.get(self.alpha))
        self.assertEqual(self.store.get(self.beta), BETA_KEY)

    def test_missing_master_never_overwrites_ciphertext_or_generates_new_master(self):
        self.store.save(self.alpha, ALPHA_KEY)
        old = self.store.key_path.read_bytes()
        self.store.key_path.unlink()
        self.assert_unavailable(lambda: self.store.get(self.alpha))
        self.assert_unavailable(lambda: self.store.save(self.beta, BETA_KEY))
        self.assertFalse(self.store.key_path.exists())
        self.assertTrue(self.store.has(self.alpha))
        self.assertFalse(self.store.has(self.beta))
        self.store.key_path.write_bytes(old)
        self.store.key_path.chmod(0o600)
        self.assertEqual(self.store.get(self.alpha), ALPHA_KEY)

    def test_wide_permissions_symlinks_and_malformed_key_fail_closed(self):
        self.store.save(self.alpha, ALPHA_KEY)
        self.store.key_path.chmod(0o644)
        self.assert_unavailable(lambda: self.store.get(self.alpha))
        self.store.key_path.chmod(0o600)
        other = self.folder / "other.key"
        self.store.key_path.rename(other)
        self.store.key_path.symlink_to(other)
        self.assert_unavailable(lambda: self.store.get(self.alpha))
        self.assert_unavailable(lambda: self.store.save(self.beta, BETA_KEY))
        self.store.key_path.unlink()
        self.store.key_path.write_bytes(b"not-the-master-key")
        self.store.key_path.chmod(0o600)
        self.assert_unavailable(lambda: self.store.get(self.alpha))
        self.assertEqual(self.store.key_path.read_bytes(), b"not-the-master-key")
        self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o600)

    def test_ciphertext_is_bounded_to_account_and_disabled_account_is_unreadable(self):
        self.store.save(self.alpha, ALPHA_KEY)
        self.store.save(self.beta, BETA_KEY)
        with self.registry.connection(write=True) as conn:
            conn.execute("""
                UPDATE auth_groq_keys SET ciphertext = (SELECT ciphertext FROM auth_groq_keys WHERE user_id = ?)
                WHERE user_id = ?
            """, (self.alpha, self.beta))
        self.assert_unavailable(lambda: self.store.get(self.beta))
        self.assertEqual(self.store.get(self.alpha), ALPHA_KEY)

        self.auth.set_enabled(self.alpha, self.beta, False)
        self.assertIsNone(self.store.get(self.beta))
        with self.assertRaises(ApiError) as problem:
            self.store.save(self.beta, BETA_KEY)
        self.assertEqual(problem.exception.status, 401)

    def test_two_store_instances_cannot_overwrite_only_if_missing(self):
        another = GroqKeyStore(self.registry)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.store.save, self.alpha, ALPHA_KEY, only_if_missing=True)
            second = pool.submit(another.save, self.alpha, BETA_KEY, only_if_missing=True)
        wins = [first.result(), second.result()]
        self.assertEqual(wins.count(True), 1)
        self.assertEqual(self.store.get(self.alpha), ALPHA_KEY if wins[0] else BETA_KEY)
        master = self.store.key_path.read_bytes()
        another.save(self.beta, BETA_KEY)
        self.assertTrue(self.store.save(self.alpha, ALPHA_KEY))
        self.assertEqual(self.store.key_path.read_bytes(), master)
        self.assertEqual(self.store.get(self.alpha), ALPHA_KEY)
        self.assertEqual(self.store.get(self.beta), BETA_KEY)
        self.assertEqual([item.name for item in self.registry.folder.glob(".groq-*")], [])

    def test_optional_mode_can_forget_key_without_secret_material(self):
        self.store.save(self.alpha, ALPHA_KEY)
        self.store.save(self.beta, BETA_KEY)
        with patch("apps.web.provider_keys._load_fernet", return_value=None):
            disabled = GroqKeyStore(self.registry)
        self.assertTrue(disabled.has(self.alpha))
        self.assert_unavailable(lambda: disabled.get(self.alpha))
        disabled.remove(self.alpha)
        self.assertIsNone(self.store.get(self.alpha))
        self.assertEqual(self.store.get(self.beta), BETA_KEY)

    def test_providers_persist_change_and_remove_independently(self):
        openai = OpenAIKeyStore(self.registry)
        self.assertFalse(openai.has(self.alpha))
        self.assertTrue(self.store.save(self.alpha, ALPHA_KEY))
        self.assertTrue(openai.save(self.alpha, f" {OPENAI_ALPHA_KEY} ", only_if_missing=True))
        self.assertIsNone(openai.get(self.beta))
        self.assertTrue(openai.save(self.beta, OPENAI_BETA_KEY))

        restarted = OpenAIKeyStore(self.registry)
        self.assertEqual(restarted.get(self.alpha), OPENAI_ALPHA_KEY)
        self.assertEqual(restarted.get(self.beta), OPENAI_BETA_KEY)
        self.assertEqual(GroqKeyStore(self.registry).get(self.alpha), ALPHA_KEY)
        self.assertNotEqual(restarted.key_path, self.store.key_path)
        self.assertNotEqual(restarted.key_path.read_bytes(), self.store.key_path.read_bytes())
        self.assertEqual(stat.S_IMODE(restarted.key_path.stat().st_mode), 0o600)

        self.assertFalse(restarted.save(self.alpha, OPENAI_REPLACEMENT_KEY, only_if_missing=True))
        self.assertEqual(openai.get(self.alpha), OPENAI_ALPHA_KEY)
        self.assertTrue(restarted.save(self.alpha, OPENAI_REPLACEMENT_KEY))
        self.assertEqual(openai.get(self.alpha), OPENAI_REPLACEMENT_KEY)
        self.assertEqual(self.store.get(self.alpha), ALPHA_KEY)
        self.assertEqual(openai.get(self.beta), OPENAI_BETA_KEY)
        self.assertTrue(self.store.save(self.alpha, BETA_KEY))
        self.assertEqual(self.store.get(self.alpha), BETA_KEY)
        self.assertEqual(openai.get(self.alpha), OPENAI_REPLACEMENT_KEY)

        registry_file = self.registry.path.read_bytes()
        with self.registry.connection() as conn:
            dump = "\n".join(conn.iterdump())
        for secret in (ALPHA_KEY, BETA_KEY, OPENAI_ALPHA_KEY, OPENAI_BETA_KEY, OPENAI_REPLACEMENT_KEY):
            self.assertNotIn(secret.encode(), registry_file)
            self.assertNotIn(secret, dump)
            self.assertNotIn(secret.encode().hex().upper(), dump)
        self.store.remove(self.alpha)
        self.assertIsNone(self.store.get(self.alpha))
        self.assertEqual(openai.get(self.alpha), OPENAI_REPLACEMENT_KEY)
        self.store.save(self.alpha, ALPHA_KEY)
        restarted.remove(self.alpha)
        self.assertIsNone(openai.get(self.alpha))
        self.assertEqual(openai.get(self.beta), OPENAI_BETA_KEY)
        self.assertEqual(self.store.get(self.alpha), ALPHA_KEY)

    def test_openai_requires_the_provider_and_user_authentication_context(self):
        openai = OpenAIKeyStore(self.registry)
        with self.assertRaises(ApiError) as error:
            openai.save(self.alpha, ALPHA_KEY)
        self.assertEqual(error.exception.field, "api_key")
        self.assertFalse(openai.key_path.exists())

        openai.save(self.alpha, OPENAI_ALPHA_KEY)
        openai.save(self.beta, OPENAI_BETA_KEY)
        with self.registry.connection(write=True) as conn:
            conn.execute("""
                UPDATE auth_openai_keys SET ciphertext = (SELECT ciphertext FROM auth_openai_keys WHERE user_id = ?)
                WHERE user_id = ?
            """, (self.alpha, self.beta))
        self.assert_unavailable(lambda: openai.get(self.beta))
        self.assertEqual(openai.get(self.alpha), OPENAI_ALPHA_KEY)

        fernet, _ = _load_fernet()
        cipher = fernet(openai.key_path.read_bytes())
        mismatched = cipher.encrypt(f"groq-v1:{self.alpha}:{OPENAI_ALPHA_KEY}".encode("ascii"))
        with self.registry.connection(write=True) as conn:
            conn.execute("UPDATE auth_openai_keys SET ciphertext = ? WHERE user_id = ?", (mismatched, self.alpha))
        self.assert_unavailable(lambda: openai.get(self.alpha))

    def test_missing_openai_master_never_affects_groq_or_recreates_the_openai_key(self):
        openai = OpenAIKeyStore(self.registry)
        self.store.save(self.alpha, ALPHA_KEY)
        openai.save(self.alpha, OPENAI_ALPHA_KEY)
        openai.key_path.unlink()
        self.assert_unavailable(lambda: openai.get(self.alpha))
        self.assert_unavailable(lambda: openai.save(self.beta, OPENAI_BETA_KEY))
        self.assertFalse(openai.key_path.exists())
        self.assertTrue(openai.has(self.alpha))
        self.assertEqual(self.store.get(self.alpha), ALPHA_KEY)

    def test_existing_groq_ciphertext_uses_the_same_keyfile_table_and_context(self):
        fernet, _ = _load_fernet()
        master = fernet.generate_key()
        self.store.key_path.write_bytes(master)
        self.store.key_path.chmod(0o600)
        legacy = fernet(master).encrypt(f"groq-v1:{self.alpha}:{ALPHA_KEY}".encode("ascii"))
        with self.registry.connection(write=True) as conn:
            conn.execute("INSERT INTO auth_groq_keys(user_id, ciphertext) VALUES (?, ?)", (self.alpha, legacy))
        openai = OpenAIKeyStore(self.registry)
        openai.save(self.alpha, OPENAI_ALPHA_KEY)
        self.assertEqual(GroqKeyStore(self.registry).get(self.alpha), ALPHA_KEY)
        self.assertEqual(self.store.key_path.name, "groq-fernet.key")
        self.assertEqual(self.store.key_path.read_bytes(), master)
        with self.registry.connection() as conn:
            stored = conn.execute("SELECT ciphertext FROM auth_groq_keys WHERE user_id = ?", (self.alpha,)).fetchone()
        self.assertEqual(stored["ciphertext"], legacy)
