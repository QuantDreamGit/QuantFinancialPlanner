"""OpenAI key lifecycle over authenticated HTTP with a mocked provider."""

import base64
import json
import os
import unittest
from contextlib import ExitStack
from datetime import date
from importlib.util import find_spec
from unittest.mock import patch

import test_auth_http as auth_http

from apps.web.values import ApiError


OWNER_KEY = "sk-project-owner-test-credential"
MEMBER_KEY = "sk-project-member-test-credential"
REPLACEMENT_KEY = "sk-project-replacement-test-credential"
SERVER_KEY = "sk-project-server-test-credential"
REJECTED_KEY = "sk-project-rejected-test-credential"
PDF = b"%PDF-1.7\nSynthetic statement\n%%EOF"
TODAY = date(2026, 11, 20)
IMPORTS = "/api/imports"
KEY_ROUTE = IMPORTS + "/openai-key"
PASSWORD = "mock-owner-password"


@unittest.skipUnless(find_spec("cryptography"), "Saving OpenAI keys requires cryptography")
class OpenAIKeyHttpTests(unittest.TestCase):
    start = auth_http.AuthHttpTests.start

    def setUp(self):
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.dict(os.environ))
        os.environ.pop("OPENAI_API_KEY", None)
        clock = patches.enter_context(patch("apps.web.__main__.date"))
        clock.today.return_value = TODAY
        row = {
            "date": "2026-11-18", "description": "Synthetic purchase", "amount": "-12.34", "currency": "EUR",
            "reference": "BANK-42", "page": 1, "evidence": "2026-11-18 Synthetic purchase -12.34", "issue": None,
        }
        parsed = {
            "rows": [row], "document_type": "statement", "total_debit": "12.34", "total_credit": "0.00",
            "document_note": None,
        }
        envelope = {"status": "completed", "output": [{
            "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": json.dumps(parsed)}],
        }]}
        self.provider = patches.enter_context(patch("apps.web.import_openai._request", return_value=json.dumps(envelope).encode()))
        self.start()
        _, headers = self.request("POST", "/api/auth/setup", data={
            "username": "owner", "password": PASSWORD, "setup_code": auth_http.SETUP_CODE,
        })
        self.owner = self.cookie(headers)
        self.account = self.request("GET", IMPORTS + "/setup", cookie=self.owner)[0]["accounts"][0]["id"]

    def request(self, method, path, **kwargs):
        result, headers = auth_http.AuthHttpTests.request(self, method, path, **kwargs)
        for key in (OWNER_KEY, MEMBER_KEY, REPLACEMENT_KEY, SERVER_KEY, REJECTED_KEY):
            self.assertNotIn(key, json.dumps(result))
            self.assertNotIn(key, str(headers))
        return result, headers

    @staticmethod
    def cookie(headers):
        return headers["Set-Cookie"].split(";", 1)[0]

    def status(self, **scope):
        return self.request("GET", IMPORTS + "/setup", **scope)[0]["openai"]

    def preview(self, account_id, *, expected=200, consent=True, api_key=None, model=None, **scope):
        data = {"source": "pdf", "pdf_method": "openai", "file": base64.b64encode(PDF).decode(),
                "filename": "November.pdf", "account_id": account_id}
        if consent is not None:
            data["ai_consent"] = consent
        if api_key is not None:
            data["api_key"] = api_key
        if model is not None:
            data["openai_model"] = model
        return self.request("POST", IMPORTS + "/preview", expected=expected, data=data, **scope)[0]

    def household(self):
        member = self.request("POST", "/api/workspaces", expected=201, cookie=self.owner,
                              data={"name": "Partner", "kind": "profile"})[0]["id"]
        group = self.request("POST", "/api/workspaces", expected=201, cookie=self.owner, data={
            "name": "Family", "kind": "group", "members": [1, member],
        })[0]["id"]
        invitation = self.request("POST", "/api/auth/invitations", expected=201, cookie=self.owner,
                                  data={"profile_id": member})[0]["token"]
        _, headers = self.request("POST", "/api/auth/accept", data={
            "username": "partner", "password": "mock-member-password", "code": invitation,
        })
        return self.cookie(headers), group

    def test_first_success_saves_key_and_change_and_removal_apply_to_subsequent_imports(self):
        self.assertFalse(self.status(cookie=self.owner)["configured"])
        first = self.preview(self.account, cookie=self.owner, api_key=OWNER_KEY)
        self.assertTrue(first["ai"]["saved_key"])
        self.provider.assert_called_once_with(PDF, OWNER_KEY, "gpt-4.1-mini")

        self.request("POST", "/api/auth/logout", cookie=self.owner, data={})
        _, headers = self.request("POST", "/api/auth/login", data={"username": "owner", "password": PASSWORD})
        cookie = self.cookie(headers)
        self.preview(self.account, cookie=cookie)
        self.provider.assert_called_with(PDF, OWNER_KEY, "gpt-4.1-mini")

        updated = self.request("PUT", KEY_ROUTE, cookie=cookie, data={"api_key": REPLACEMENT_KEY})[0]["ai"]
        self.assertTrue(updated["saved_key"])
        self.preview(self.account, cookie=cookie)
        self.provider.assert_called_with(PDF, REPLACEMENT_KEY, "gpt-4.1-mini")

        removed = self.request("DELETE", KEY_ROUTE, cookie=cookie)[0]["ai"]
        self.assertFalse(removed["saved_key"])
        self.assertFalse(self.status(cookie=cookie)["configured"])
        self.preview(self.account, cookie=cookie, expected=422)
        self.assertEqual(self.provider.call_count, 3)

        with patch.dict(os.environ, {"OPENAI_API_KEY": SERVER_KEY}):
            fallback = self.preview(self.account, cookie=cookie)["ai"]
        self.assertTrue(fallback["server_key"])
        self.assertFalse(fallback["saved_key"])
        self.provider.assert_called_with(PDF, SERVER_KEY, "gpt-4.1-mini")

    def test_csv_and_pdf_share_the_saved_openai_key(self):
        layout = {"mapping": {"date": 0, "name": 1, "amount": 2},
                  "date_format": "ymd", "amount_mode": "debit-negative"}
        data = {"source": "csv", "csv_method": "openai", "ai_consent": True, "account_id": self.account,
                "file": base64.b64encode(b"Date,Name,Amount\n2026-11-18,Fare,-5.00").decode()}
        with patch("apps.web.imports.suggest_csv_mapping", return_value=layout) as suggest:
            first, _ = self.request("POST", IMPORTS + "/preview", data=dict(data, api_key=OWNER_KEY), cookie=self.owner)
            self.assertTrue(first["ai"]["saved_key"])
            self.request("POST", IMPORTS + "/preview", data=data, cookie=self.owner)
        self.assertEqual([call.kwargs["api_key"] for call in suggest.call_args_list], [OWNER_KEY, OWNER_KEY])
        preview = self.preview(self.account, cookie=self.owner, model="gpt-5.4")
        self.provider.assert_called_once_with(PDF, OWNER_KEY, "gpt-5.4")
        self.assertEqual(preview["openai_model"], "gpt-5.4")

    def test_members_of_the_same_workspace_use_only_their_own_credentials(self):
        partner, group = self.household()
        joint = self.request("POST", "/api/accounts", expected=201, cookie=self.owner, workspace=group, data={
            "name": "Joint account", "kind": "checking", "opened_on": "2026-10-01", "opening_balance": "500",
        })[0]["id"]
        self.request("PUT", KEY_ROUTE, cookie=self.owner, data={"api_key": OWNER_KEY})
        self.assertTrue(self.status(cookie=self.owner, workspace=group)["saved_key"])
        self.assertFalse(self.status(cookie=partner, workspace=group)["saved_key"])
        self.preview(joint, cookie=partner, workspace=group, expected=422)
        self.provider.assert_not_called()

        self.preview(joint, cookie=partner, workspace=group, api_key=MEMBER_KEY)
        self.assertTrue(self.status(cookie=partner)["saved_key"])
        self.provider.assert_called_with(PDF, MEMBER_KEY, "gpt-4.1-mini")
        self.preview(joint, cookie=self.owner, workspace=group)
        self.provider.assert_called_with(PDF, OWNER_KEY, "gpt-4.1-mini")

        self.request("DELETE", KEY_ROUTE, cookie=partner, workspace=group)
        self.assertFalse(self.status(cookie=partner)["saved_key"])
        self.assertTrue(self.status(cookie=self.owner, workspace=group)["saved_key"])
        self.preview(joint, cookie=self.owner, workspace=group)
        self.provider.assert_called_with(PDF, OWNER_KEY, "gpt-4.1-mini")

    def test_unauthenticated_or_unconsented_requests_never_reach_provider(self):
        self.request("GET", IMPORTS + "/setup", expected=401)
        self.request("PUT", KEY_ROUTE, expected=401, data={"api_key": OWNER_KEY})
        self.request("DELETE", KEY_ROUTE, expected=401)
        self.preview(self.account, expected=401, api_key=OWNER_KEY)
        error = self.preview(self.account, cookie=self.owner, expected=422, consent=False, api_key=OWNER_KEY)
        self.assertEqual(error["error"]["field"], "ai_consent")
        self.assertFalse(self.status(cookie=self.owner)["saved_key"])
        self.request("PUT", KEY_ROUTE, cookie=self.owner, data={"api_key": OWNER_KEY})
        for consent in (None, "true"):
            with self.subTest(consent=consent):
                error = self.preview(self.account, cookie=self.owner, expected=422, consent=consent)
                self.assertEqual(error["error"]["field"], "ai_consent")
        self.provider.assert_not_called()

    def test_provider_rejection_does_not_remember_key(self):
        self.provider.side_effect = ApiError("Provider rejected the key.", "api_key")
        result = self.preview(self.account, cookie=self.owner, expected=422, api_key=REJECTED_KEY)
        self.assertEqual(result["error"]["field"], "api_key")
        self.assertFalse(self.status(cookie=self.owner)["saved_key"])
        self.provider.assert_called_once_with(PDF, REJECTED_KEY, "gpt-4.1-mini")
