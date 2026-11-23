"""Saved Groq credentials remain private across authenticated workspace views."""

import base64
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date
from http.client import HTTPConnection
from importlib.util import find_spec
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from apps.web.__main__ import DashboardServer
from apps.web.storage import Database
from apps.web.values import ApiError


SETUP = "synthetic-server-setup-0123456789"
OWNER_KEY = "TEST_INTEGRATION_OWNER_KEY"
MEMBER_KEY = "TEST_INTEGRATION_MEMBER_KEY"
SERVER_KEY = "TEST_INTEGRATION_SERVER_KEY"
REJECTED_KEY = "TEST_INTEGRATION_REJECTED_KEY"
PDF = base64.b64encode(b"%PDF-1.4 synthetic fixture").decode("ascii")
TEXT = "2026-11-18 Synthetic purchase -12.34 EUR"
TODAY = date(2026, 11, 20)


@unittest.skipUnless(find_spec("cryptography"), "Saving Groq keys requires cryptography")
class GroqKeyHttpTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        database = Database(Path(folder.name) / "ledger.sqlite3")
        database.initialize()
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(patch.dict(os.environ))
        os.environ.pop("GROQ_API_KEY", None)
        clock = contexts.enter_context(patch("apps.web.__main__.date"))
        clock.today.return_value = TODAY
        contexts.enter_context(patch("apps.web.imports._pdf_text", return_value=TEXT))
        self.provider = contexts.enter_context(patch("apps.web.import_ai._request"))
        self.provider.return_value = json.dumps({
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"rows": [{
                "source_line": 1, "date": "2026-11-18", "description": "Synthetic purchase",
                "amount": "-12.34", "currency": "EUR", "reference": "",
            }]})}}],
        }).encode()

        self.server = DashboardServer(0, database, None, setup_code=SETUP)
        self.addCleanup(self.server.server_close)
        worker = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.addCleanup(worker.join)
        self.addCleanup(self.server.shutdown)
        worker.start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        _, headers = self.request("POST", "/api/auth/setup", data={
            "username": "owner", "password": "test-owner-password", "setup_code": SETUP,
        })
        self.owner = headers["Set-Cookie"].split(";", 1)[0]
        self.owner_bank = self.bank("Owner bank", self.owner)

    def request(self, method, path, *, cookie=None, workspace=None, data=None, expected=200):
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if method != "GET":
            headers["Origin"] = self.origin
        if cookie:
            headers["Cookie"] = cookie
        if workspace is not None:
            headers["X-Workspace"] = str(workspace)
        if data is not None:
            headers["Content-Type"] = "application/json"
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None, headers)
            response = connection.getresponse()
            raw = response.read().decode("utf-8")
            for key in (OWNER_KEY, MEMBER_KEY, SERVER_KEY, REJECTED_KEY):
                self.assertNotIn(key, raw)
                self.assertNotIn(key, str(response.headers))
            self.assertEqual(response.status, expected, (method, path, raw))
            return json.loads(raw), response.headers
        finally:
            connection.close()

    def bank(self, name, cookie, workspace=None):
        return self.request("POST", "/api/accounts", cookie=cookie, workspace=workspace, expected=201, data={
            "name": name, "kind": "checking", "opened_on": "2026-10-01", "opening_balance": "100",
        })[0]["id"]

    def status(self, cookie, workspace=None):
        return self.request("GET", "/api/imports/setup", cookie=cookie, workspace=workspace)[0]["ai"]

    def save(self, cookie, key, workspace=None):
        return self.request("PUT", "/api/imports/groq-key", cookie=cookie, workspace=workspace, data={"api_key": key})[0]["ai"]

    def remove(self, cookie, workspace=None):
        return self.request("DELETE", "/api/imports/groq-key", cookie=cookie, workspace=workspace)[0]["ai"]

    def preview(self, cookie, account, workspace=None, expected=200, **extra):
        return self.request("POST", "/api/imports/preview", cookie=cookie, workspace=workspace, expected=expected, data={
            "source": "pdf", "account_id": account, "file": PDF, "pdf_method": "ai", "ai_consent": True, **extra,
        })[0]

    def household(self):
        partner = self.request("POST", "/api/workspaces", cookie=self.owner, expected=201, data={
            "kind": "profile", "name": "Partner",
        })[0]["id"]
        group = self.request("POST", "/api/workspaces", cookie=self.owner, expected=201, data={
            "kind": "group", "name": "Household", "members": [1, partner],
        })[0]["id"]
        invitation = self.request("POST", "/api/auth/invitations", cookie=self.owner, expected=201, data={
            "profile_id": partner,
        })[0]
        _, headers = self.request("POST", "/api/auth/accept", data={
            "username": "partner", "password": "test-partner-password", "code": invitation["token"],
        })
        return headers["Set-Cookie"].split(";", 1)[0], group

    def test_secret_management_requires_authentication_and_never_returns_plaintext(self):
        self.request("GET", "/api/imports/setup", expected=401)
        self.request("PUT", "/api/imports/groq-key", data={"api_key": OWNER_KEY}, expected=401)
        self.request("DELETE", "/api/imports/groq-key", expected=401)
        self.assertEqual(self.status(self.owner), {
            "configured": False, "provider": "Groq", "saved_key": False, "server_key": False, "can_save_key": True,
        })
        self.assertTrue(self.save(self.owner, OWNER_KEY)["saved_key"])
        state = self.status(self.owner)
        self.assertTrue(state["saved_key"])
        self.assertTrue(state["configured"])
        self.provider.assert_not_called()
        with self.server.workspaces.connection() as connection:
            self.assertNotIn(OWNER_KEY, "\n".join(connection.iterdump()))
        self.assertFalse(self.remove(self.owner)["saved_key"])
        self.assertFalse(self.status(self.owner)["configured"])

    def test_first_success_remembers_key_and_each_pdf_still_needs_consent(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": SERVER_KEY}):
            self.assertTrue(self.status(self.owner)["server_key"])
            initial = self.preview(self.owner, self.owner_bank, api_key=OWNER_KEY)
            self.assertTrue(initial["ai"]["saved_key"])
            self.assertEqual(self.provider.call_args.args[1], OWNER_KEY)
            self.assertEqual(self.provider.call_count, 1)
            self.preview(self.owner, self.owner_bank, expected=422, ai_consent=False)
            self.assertEqual(self.provider.call_count, 1)
            resumed = self.preview(self.owner, self.owner_bank)
            self.assertTrue(resumed["ai"]["saved_key"])
            self.assertEqual(self.provider.call_args.args[1], OWNER_KEY)
            self.preview(self.owner, self.owner_bank, api_key=MEMBER_KEY)
            self.assertEqual(self.provider.call_args.args[1], MEMBER_KEY)
            self.preview(self.owner, self.owner_bank)
            self.assertEqual(self.provider.call_args.args[1], OWNER_KEY)
            state = self.remove(self.owner)
            self.assertFalse(state["saved_key"])
            self.assertTrue(state["configured"])
            fallback = self.preview(self.owner, self.owner_bank)
            self.assertEqual(self.provider.call_args.args[1], SERVER_KEY)
            self.assertFalse(fallback["ai"]["saved_key"])

    def test_group_members_cannot_view_or_replace_each_others_key(self):
        partner, group = self.household()
        joint = self.bank("Joint bank", self.owner, workspace=group)
        self.save(self.owner, OWNER_KEY)
        self.assertTrue(self.status(self.owner, group)["saved_key"])
        self.assertFalse(self.status(partner, group)["saved_key"])
        self.assertFalse(self.status(partner)["configured"])
        self.save(partner, MEMBER_KEY, workspace=group)
        self.assertTrue(self.status(partner)["saved_key"])
        self.preview(partner, joint, workspace=group)
        self.assertEqual(self.provider.call_args.args[1], MEMBER_KEY)
        self.preview(self.owner, joint, workspace=group)
        self.assertEqual(self.provider.call_args.args[1], OWNER_KEY)
        self.remove(partner, workspace=group)
        self.assertFalse(self.status(partner)["saved_key"])
        self.assertTrue(self.status(self.owner)["saved_key"])
        self.preview(self.owner, self.owner_bank)
        self.assertEqual(self.provider.call_args.args[1], OWNER_KEY)

    def test_invalid_or_rejected_key_does_not_auto_save(self):
        self.preview(self.owner, self.owner_bank, api_key="!", expected=422)
        self.provider.assert_not_called()
        self.assertFalse(self.status(self.owner)["saved_key"])
        self.provider.side_effect = ApiError("The mock provider rejected the key.", "api_key")
        self.preview(self.owner, self.owner_bank, api_key=REJECTED_KEY, expected=422)
        self.assertEqual(self.provider.call_args.args[1], REJECTED_KEY)
        self.assertFalse(self.status(self.owner)["saved_key"])
        self.assertFalse(self.status(self.owner)["configured"])
