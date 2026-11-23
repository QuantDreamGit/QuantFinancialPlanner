"""Sign-in and workspace isolation through the real HTTP server."""

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from apps.web.__main__ import DashboardServer
from apps.web.starter_categories import STARTER_CATEGORIES
from apps.web.storage import Database


SETUP_CODE = "temporary-test-setup-token-0123456789"


class AuthHttpTests(unittest.TestCase):
    def test_registration_creates_an_isolated_profile_and_signs_it_in(self):
        self.start()
        credentials = {"username": "Sam", "password": "registration test password", "starter_categories": True}
        self.request("POST", "/api/auth/register", data=credentials, expected=403)
        _, headers = self.request("POST", "/api/auth/setup",
                                  data={"username": "owner", "password": "!", "setup_code": SETUP_CODE})
        owner = headers["Set-Cookie"].split(";", 1)[0]
        reserved, _ = self.request("POST", "/api/workspaces", cookie=owner, expected=201,
                                   data={"name": "Sam", "kind": "profile"})
        family, _ = self.request("POST", "/api/workspaces", cookie=owner, expected=201,
                                 data={"name": "Family", "kind": "group", "members": [1, reserved["id"]]})

        new, headers = self.request("POST", "/api/auth/register", expected=201,
                                    data=dict(credentials, profile_id=reserved["id"], is_admin=True))
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        profile = new["profile_id"]
        self.assertNotIn(profile, (1, reserved["id"]))
        self.assertEqual((new["username"], new["is_admin"]), ("sam", False))
        workspaces, _ = self.request("GET", "/api/workspaces", cookie=cookie)
        self.assertEqual([row["id"] for row in workspaces["workspaces"]], [profile])
        self.assertEqual(workspaces["active"]["name"], "sam (2)")
        dashboard, _ = self.request("GET", "/api/dashboard", cookie=cookie)
        self.assertEqual(dashboard["accounts"], [])
        self.assertEqual(len(dashboard["categories"]), len(STARTER_CATEGORIES))
        self.request("GET", "/api/auth/members", expected=403, cookie=cookie)
        for workspace in (1, reserved["id"], family["id"]):
            self.request("GET", "/api/dashboard", expected=403, cookie=cookie, workspace=workspace)

        duplicate, _ = self.request("POST", "/api/auth/register", data=dict(credentials, username=" SAM "), expected=409)
        self.assertEqual(duplicate["error"]["field"], "username")
        roster, _ = self.request("GET", "/api/auth/members", cookie=owner)
        self.assertEqual({row["id"] for row in roster["profiles"]}, {1, reserved["id"], profile})

        self.request("POST", "/api/auth/logout", cookie=cookie, data={})
        signed_in, headers = self.request("POST", "/api/auth/login", data=credentials)
        self.assertEqual(signed_in["profile_id"], profile)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.request("PUT", f"/api/workspaces/{family['id']}", cookie=owner,
                     data={"name": "Family", "kind": "group", "members": [1, reserved["id"], profile]})
        self.request("GET", "/api/dashboard", cookie=cookie, workspace=family["id"])

    def test_cars_share_one_history_across_people_and_groups(self):
        self.start()
        _, headers = self.request("POST", "/api/auth/setup",
                                  data={"username": "owner", "password": "!", "setup_code": SETUP_CODE})
        owner = headers["Set-Cookie"].split(";", 1)[0]

        def add_person(name):
            person, _ = self.request("POST", "/api/workspaces", cookie=owner, expected=201,
                                     data={"kind": "profile", "name": name})
            invite, _ = self.request("POST", "/api/auth/invitations", cookie=owner, expected=201,
                                     data={"profile_id": person["id"]})
            _, headers = self.request("POST", "/api/auth/accept",
                                      data={"code": invite["token"], "username": name.lower(), "password": "!"})
            return person["id"], headers["Set-Cookie"].split(";", 1)[0]

        emma_id, emma = add_person("Emma")
        alex_id, alex = add_person("Alex")
        group, _ = self.request("POST", "/api/workspaces", cookie=owner, expected=201,
                                data={"kind": "group", "name": "Family", "members": [1, emma_id]})
        family = group["id"]
        road_group = {"kind": "group", "name": "Road Trip", "members": [1, alex_id]}
        road, _ = self.request("POST", "/api/workspaces", cookie=owner, expected=201, data=road_group)
        config = {"name": "Panda", "drivetrain": "4wd"}
        panda, _ = self.request("POST", "/api/cars", cookie=owner, expected=201, data=config)
        self.request("GET", f"/api/cars/{panda['id']}", cookie=emma, expected=404)
        self.request("GET", f"/api/cars/{panda['id']}", cookie=emma, workspace=family, expected=404)

        def car_ids(cookie, workspace=None):
            response, _ = self.request("GET", "/api/cars", cookie=cookie, workspace=workspace)
            return {car["id"] for car in response["cars"]}

        golf, _ = self.request("POST", "/api/cars", cookie=emma, expected=201,
                               data={"name": "Golf", "drivetrain": "fwd", "workspace_ids": [emma_id, family]})
        path = f"/api/cars/{panda['id']}"
        self.request("PUT", path, cookie=owner, data=dict(config, workspace_ids=[1, family, road["id"]]))
        self.assertEqual(car_ids(owner), {panda["id"]})
        self.assertEqual(car_ids(emma), {golf["id"]})
        self.assertEqual(car_ids(emma, family), {panda["id"], golf["id"]})
        self.request("GET", path, cookie=alex, workspace=family, expected=403)
        self.request("GET", path, cookie=alex, expected=404)
        self.assertEqual(car_ids(alex, road["id"]), {panda["id"]})
        self.request("PUT", f"/api/workspaces/{road['id']}", cookie=owner,
                     data=dict(road_group, members=[1, emma_id]))
        self.request("GET", path, cookie=alex, workspace=road["id"], expected=403)

        self.request("POST", path + "/activities", cookie=emma, workspace=family, expected=201, data={
            "kind": "odometer", "recorded_on": date.today().isoformat(), "odometer_km": 98765,
        })
        for car_id, amount in [(panda["id"], "40"), (golf["id"], "50")]:
            self.request("POST", f"/api/cars/{car_id}/activities", cookie=emma, workspace=family, expected=201, data={
                "kind": "fuel", "recorded_on": date.today().isoformat(), "odometer_km": 98765, "litres": "20", "cost": amount,
            })
        private, _ = self.request("POST", "/api/cars", cookie=owner, expected=201, data=dict(config, name="Private car"))
        self.request("POST", f"/api/cars/{private['id']}/activities", cookie=owner, expected=201, data={
            "kind": "fuel", "recorded_on": date.today().isoformat(), "odometer_km": 100000, "litres": "40", "cost": "80",
        })
        family_report, _ = self.request("GET", "/api/cars", cookie=emma, workspace=family)
        self.assertEqual(family_report["group_costs"]["actual"]["fuel"], "9000")
        self.assertEqual({row["car_id"] for row in family_report["group_costs"]["cars"]}, {panda["id"], golf["id"]})
        own_garage, _ = self.request("GET", "/api/cars", cookie=owner)
        self.assertIsNone(own_garage["group_costs"])
        self.request("DELETE", f"/api/cars/{private['id']}", cookie=owner)
        personal, _ = self.request("GET", path, cookie=owner)
        shared, _ = self.request("GET", path, cookie=emma, workspace=family)
        self.assertEqual(personal["odometer"]["odometer_km"], 98765)
        self.assertEqual(shared["odometer"], personal["odometer"])
        self.assertFalse(shared["car"]["can_manage"])
        self.request("PUT", path, cookie=emma, workspace=family, expected=403,
                     data=dict(config, workspace_ids=[emma_id]))
        self.request("DELETE", path, cookie=emma, workspace=family, expected=403)
        self.request("PUT", path, cookie=owner, data=dict(config, workspace_ids=[1, emma_id, family]))
        self.assertEqual(car_ids(emma), {panda["id"], golf["id"]})
        self.request("PUT", path, cookie=emma, expected=422, data=dict(config, workspace_ids=[alex_id]))
        self.request("PUT", path, cookie=owner, expected=422, data=dict(config, workspace_ids=[]))
        self.request("PUT", path, cookie=owner, data=dict(config, workspace_ids=[1]))
        self.request("GET", path, cookie=emma, workspace=family, expected=404)
        self.request("POST", path + "/activities", cookie=emma, workspace=family, expected=404, data={})
        self.assertEqual(car_ids(emma), {golf["id"]})

        van, _ = self.request("POST", "/api/cars", cookie=emma, workspace=family, expected=201,
                              data={"name": "Van", "drivetrain": "rwd"})
        self.assertEqual(car_ids(emma, family), {golf["id"], van["id"]})
        self.assertEqual(car_ids(emma), {golf["id"]})
        self.assertEqual(car_ids(owner), {panda["id"]})
        self.request("PUT", f"/api/cars/{van['id']}", cookie=owner, workspace=family,
                     data={"name": "Van", "drivetrain": "rwd", "workspace_ids": [emma_id]})
        self.assertEqual(car_ids(emma), {golf["id"], van["id"]})
        self.assertEqual(car_ids(owner, family), {golf["id"]})

    def test_starter_categories_opt_in_targets_only_the_authorized_ledger(self):
        self.start()
        setup = {"username": "owner", "password": "!", "setup_code": SETUP_CODE}
        invalid, _ = self.request("POST", "/api/auth/setup", expected=422, data=dict(setup, starter_categories="true"))
        self.assertEqual(invalid["error"]["field"], "starter_categories")
        state, headers = self.request("POST", "/api/auth/setup", data=dict(setup, starter_categories=True, profile_id=17))
        self.assertEqual(state["profile_id"], 1)
        owner = headers["Set-Cookie"].split(";", 1)[0]
        count = len(STARTER_CATEGORIES)
        dash, _ = self.request("GET", "/api/dashboard", cookie=owner)
        self.assertEqual(len(dash["categories"]), count)
        self.assertEqual(len(dash["accounts"]), 1)

        member, _ = self.request("POST", "/api/workspaces", expected=201, cookie=owner,
                                 data={"kind": "profile", "name": "Jamie", "starter_categories": False})
        other, _ = self.request("POST", "/api/workspaces", expected=201, cookie=owner,
                                data={"kind": "profile", "name": "Taylor", "starter_categories": True})
        group, _ = self.request("POST", "/api/workspaces", expected=201, cookie=owner, data={
            "kind": "group", "name": "Family", "members": [1, member["id"]], "starter_categories": True,
        })
        invite, _ = self.request("POST", "/api/auth/invitations", expected=201, cookie=owner,
                                 data={"profile_id": member["id"]})
        signup = {"code": invite["token"], "username": "jamie", "password": "?", "profile_id": other["id"]}
        self.request("POST", "/api/auth/accept", expected=422, data=dict(signup, starter_categories=None))
        state, headers = self.request("POST", "/api/auth/accept", workspace=group["id"],
                                      data=dict(signup, starter_categories=True))
        self.assertEqual(state["profile_id"], member["id"])
        jamie = headers["Set-Cookie"].split(";", 1)[0]
        personal, _ = self.request("GET", "/api/dashboard", cookie=jamie)
        self.assertEqual(len(personal["categories"]), count)
        combined, _ = self.request("GET", "/api/dashboard", cookie=jamie, workspace=group["id"])
        self.assertEqual(sum(row["id"] > 0 for row in combined["categories"]), count)
        self.assertEqual(len(combined["categories"]), count * 3)
        self.request("POST", "/api/categories/starter", cookie=jamie, workspace=1, expected=403, data={})
        preset, _ = self.request("GET", "/api/categories/starter", cookie=jamie)
        self.assertEqual(len(preset["categories"]), count)
        repeated, _ = self.request("POST", "/api/categories/starter", cookie=jamie, data={})
        self.assertEqual(repeated, {"added": 0})
        bonds = next(row["id"] for row in combined["categories"] if row["id"] > 0 and row["name"] == "Bonds")
        self.request("DELETE", f"/api/categories/{bonds}", cookie=jamie, workspace=group["id"])
        restored, _ = self.request("POST", "/api/categories/starter", cookie=jamie, workspace=group["id"], data={})
        self.assertEqual(restored, {"added": 1})

        invite, _ = self.request("POST", "/api/auth/invitations", expected=201, cookie=owner,
                                 data={"profile_id": other["id"]})
        _, headers = self.request("POST", "/api/auth/accept",
                                  data={"code": invite["token"], "username": "taylor", "password": "?"})
        taylor = headers["Set-Cookie"].split(";", 1)[0]
        preconfigured, _ = self.request("GET", "/api/dashboard", cookie=taylor)
        self.assertEqual(len(preconfigured["categories"]), count)

    def test_cli_reports_occupied_ports_and_exits_without_starting(self):
        environment = os.environ.copy()
        environment.pop("QFP_SETUP_CODE", None)
        with tempfile.TemporaryDirectory() as folder, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            command = [sys.executable, "-m", "apps.web", "--port", str(port), "--db", str(Path(folder) / "test.sqlite3")]
            for proxy in (False, True):
                with self.subTest(proxy=proxy):
                    extra = ["--public-url", "https://finance.example.test"] if proxy else []
                    result = subprocess.run(command + extra, cwd=Path(__file__).resolve().parents[2],
                                            env=environment, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(f"Port {port} is already in use", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    if proxy:
                        self.assertIn("update the reverse proxy upstream", result.stderr)
                        self.assertNotIn("--port 0", result.stderr)
                    else:
                        self.assertIn("--port 0", result.stderr)

    def start(self, public_url=None):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        database = Database(Path(folder.name) / "financial.sqlite3")
        database.initialize()
        database.save_account({"name": "Legacy savings", "kind": "savings", "opened_on": "2001-01-01",
                               "opening_balance": "100"}, date.today())
        server = DashboardServer(0, database, None, public_url=public_url, setup_code=SETUP_CODE)
        self.addCleanup(server.server_close)
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        worker.start()
        self.port = server.server_port
        self.origin = public_url or f"http://127.0.0.1:{self.port}"
        self.host = self.origin.split("//", 1)[1]

    def request(self, method, path, *, expected=200, data=None, cookie=None, workspace=None, headers=None):
        request_headers = {"Host": self.host}
        if method != "GET":
            request_headers["Origin"] = self.origin
        if cookie is not None:
            request_headers["Cookie"] = cookie
        if workspace is not None:
            request_headers["X-Workspace"] = str(workspace)
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None, request_headers)
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(response.status, expected, (method, path, body))
            if response.getheader("Content-Type").startswith("application/json"):
                body = json.loads(body)
            return body, response.headers
        finally:
            connection.close()

    def test_transaction_documents_require_auth_and_download_with_safe_headers(self):
        self.start()
        _, headers = self.request("POST", "/api/auth/setup",
                                  data={"username": "owner", "password": "!", "setup_code": SETUP_CODE})
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        transaction, _ = self.request("POST", "/api/transactions", expected=201, cookie=cookie, data={
            "account_id": 1, "name": "Repair", "kind": "expense", "amount": "35",
            "posted_on": date.today().isoformat(),
        })
        base = f"/api/transactions/{transaction['id']}/attachments"
        invoice = b"%PDF-1.4\nSynthetic invoice\n"
        document, _ = self.request("POST", base, expected=201, cookie=cookie, data={
            "name": "fattura caffè.pdf", "file": base64.b64encode(invoice).decode(),
        })
        target = f"{base}/{document['id']}"
        self.request("GET", target, expected=401)
        received, headers = self.request("GET", target, cookie=cookie)
        self.assertEqual(received, invoice)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn("attachment;", headers["Content-Disposition"])
        self.assertIn("filename*=UTF-8''fattura%20caff%C3%A8.pdf", headers["Content-Disposition"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        entries, _ = self.request("GET", "/api/transactions", cookie=cookie)
        self.assertEqual(entries["entries"][0]["attachment_count"], 1)
        self.request("DELETE", target, cookie=cookie)
        self.request("GET", target, expected=404, cookie=cookie)

    def test_funding_check_finds_a_later_approved_transfer_before_payday(self):
        self.start()
        with patch("apps.web.__main__.date") as clock:
            clock.today.return_value = date(2026, 11, 23)
            _, headers = self.request("POST", "/api/auth/setup",
                                      data={"username": "owner", "password": "!", "setup_code": SETUP_CODE})
            cookie = headers["Set-Cookie"].split(";", 1)[0]
            accounts = {}
            for name, kind, balance in (("Checking", "checking", "1450"), ("Term savings", "savings", "2700")):
                row, _ = self.request("POST", "/api/accounts", cookie=cookie, expected=201, data={
                    "name": name, "kind": kind, "opening_balance": balance, "opened_on": "2026-11-20",
                })
                accounts[name] = row["id"]
            payer, donor = accounts.values()
            self.request("PUT", f"/api/funding/accounts/{payer}", cookie=cookie, data={"reserve": "200"})
            self.request("PUT", f"/api/funding/accounts/{donor}", cookie=cookie, data={
                "reserve": "400", "available_on": "2026-11-25",
            })
            self.request("POST", "/api/funding/routes", cookie=cookie, expected=201, data={
                "source_id": donor, "destination_id": payer, "days": 1,
            })
            for name, kind, amount, when in (("Rent", "expense", "1200", "2026-11-25"),
                                               ("Payday", "income", "700", "2026-11-27")):
                self.request("POST", "/api/forecast/commitments", cookie=cookie, expected=201, data={
                    "name": name, "kind": kind, "amount": amount, "start_on": when,
                    "cadence": "once", "account_id": payer,
                })
            request = {"account_id": payer, "amount": "400", "purchase_on": "2026-11-23", "days": 10}
            response, _ = self.request("POST", "/api/funding/check", cookie=cookie, data=request)
            result = response["base"]
            self.assertFalse(result["can_fund"])
            self.assertIsNone(result["baseline_first_shortfall"])
            self.assertEqual(result["first_shortfall"]["amount"], "35000")
            self.assertEqual([event["name"] for event in result["first_shortfall"]["events"]], ["Proposed purchase", "Rent"])
            self.assertEqual((result["earliest_funded_on"], result["earliest_on"]), ("2026-11-26", "2026-11-27"))
            response, _ = self.request("POST", "/api/funding/check", cookie=cookie,
                                       data=dict(request, purchase_on="2026-11-26"))
            result = response["base"]
            self.assertTrue(result["can_fund"])
            self.assertEqual(result["moved"], "35000")
            self.assertEqual(result["transfers"][0]["arrives_on"], "2026-11-26")

    def test_persistent_sample_workspaces_are_editable_and_private_to_each_sign_in(self):
        self.start()
        self.request("POST", "/api/workspaces/demo", expected=401, data={})
        _, headers = self.request("POST", "/api/auth/setup",
                                  data={"username": "owner", "password": "!", "setup_code": SETUP_CODE})
        owner = headers["Set-Cookie"].split(";", 1)[0]
        demo, _ = self.request("POST", "/api/workspaces/demo", cookie=owner, data={})
        self.assertEqual(len(set(demo.values())), 3)
        sample_id, family_id = demo["personal_id"], demo["family_id"]
        comparisons, _ = self.request("GET", "/api/forecast/comparisons", cookie=owner, workspace=sample_id)
        self.assertTrue(comparisons)
        overlay, _ = self.request("GET", f"/api/forecast/actual-simulation/{comparisons[0]['id']}",
                                  cookie=owner, workspace=sample_id)
        self.assertTrue(any(point["actual"] is not None for point in overlay["result"]["points"]))
        setup, _ = self.request("GET", "/api/funding/setup", cookie=owner, workspace=family_id)
        self.assertTrue(setup["routes"])
        self.assertTrue(any(account["name"] == "Family checking" for account in setup["accounts"]))
        sample_check, _ = self.request("POST", "/api/funding/check", cookie=owner,
                                       workspace=family_id, data=setup["example"])
        self.assertEqual((sample_check["base"]["moved"], sample_check["late"]["moved"]), ("45000", "75000"))
        self.request("POST", "/api/accounts", expected=201, cookie=owner, workspace=sample_id, data={
            "name": "Edited Sample", "kind": "cash", "opening_balance": "123", "opened_on": date.today().isoformat(),
        })
        replay, _ = self.request("POST", "/api/workspaces/demo", cookie=owner, data={})
        self.assertEqual(replay, demo)
        data, _ = self.request("GET", "/api/dashboard", cookie=owner, workspace=sample_id)
        self.assertTrue(any(account["name"] == "Edited Sample" for account in data["accounts"]))
        data, _ = self.request("GET", "/api/dashboard", cookie=owner)
        self.assertEqual([account["name"] for account in data["accounts"]], ["Legacy savings"])
        roster, _ = self.request("GET", "/api/auth/members", cookie=owner)
        self.assertTrue(set(demo.values()).isdisjoint(row["id"] for row in roster["profiles"]))
        self.request("POST", "/api/auth/invitations", expected=422, cookie=owner, data={"profile_id": sample_id})
        self.request("POST", "/api/workspaces", expected=422, cookie=owner, data={
            "name": "Mixed group", "kind": "group", "members": [1, sample_id],
        })
        member, _ = self.request("POST", "/api/workspaces", expected=201, cookie=owner,
                                 data={"kind": "profile", "name": "Taylor"})
        invite, _ = self.request("POST", "/api/auth/invitations", expected=201, cookie=owner,
                                 data={"profile_id": member["id"]})
        _, headers = self.request("POST", "/api/auth/accept", data={"code": invite["token"], "username": "taylor", "password": "?"})
        taylor = headers["Set-Cookie"].split(";", 1)[0]
        for workspace in demo.values():
            self.request("GET", "/api/dashboard", expected=403, cookie=taylor, workspace=workspace)
        taylor_demo, _ = self.request("POST", "/api/workspaces/demo", cookie=taylor, data={})
        self.assertTrue(set(demo.values()).isdisjoint(taylor_demo.values()))

    def test_setup_invite_and_group_sharing_keep_other_personal_ledgers_private(self):
        self.start()
        self.request("GET", "/api/dashboard", expected=401)
        self.request("GET", "/auth")
        self.assertFalse(self.request("GET", "/api/auth/state")[0]["configured"])
        self.request("POST", "/api/auth/setup", expected=403,
                     data={"username": "owner", "password": "!", "setup_code": "wrong"})
        state, headers = self.request("POST", "/api/auth/setup",
                                      data={"username": "owner", "password": "!", "setup_code": SETUP_CODE})
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly; SameSite=Strict", cookie)
        self.assertTrue(state["is_admin"])
        owner = cookie.split(";", 1)[0]
        member, _ = self.request("POST", "/api/workspaces", expected=201, cookie=owner,
                                 data={"kind": "profile", "name": "Jamie"})
        third, _ = self.request("POST", "/api/workspaces", expected=201, cookie=owner,
                                data={"kind": "profile", "name": "Taylor"})
        group, _ = self.request("POST", "/api/workspaces", expected=201, cookie=owner,
                                data={"kind": "group", "name": "Family", "members": [1, member["id"]]})
        invite, _ = self.request("POST", "/api/auth/invitations", expected=201, cookie=owner,
                                 data={"profile_id": member["id"]})
        signup = {"code": invite["token"], "username": "jamie", "password": "🔑"}
        state, headers = self.request("POST", "/api/auth/accept", data=signup)
        self.assertEqual((state["profile_id"], state["is_admin"]), (member["id"], False))
        jamie = headers["Set-Cookie"].split(";", 1)[0]
        self.request("POST", "/api/auth/accept", expected=403, data=dict(signup, username="other"))

        dashboard, _ = self.request("GET", "/api/dashboard", cookie=jamie)
        self.assertEqual(dashboard["accounts"], [])
        workspace_list, _ = self.request("GET", "/api/workspaces", cookie=jamie)
        self.assertEqual({item["id"] for item in workspace_list["workspaces"]}, {member["id"], group["id"]})
        self.assertEqual(workspace_list["managed"], [])
        self.request("GET", "/api/dashboard", expected=403, cookie=jamie, workspace=1)
        self.request("GET", "/api/dashboard", expected=403, cookie=owner, workspace=member["id"])
        self.request("GET", "/api/auth/members", expected=403, cookie=jamie)
        self.request("PUT", f"/api/workspaces/{group['id']}", expected=403, cookie=jamie, data={})
        self.request("GET", "/api/dashboard", expected=401, cookie=jamie,
                     headers={"X-Profile": "1"}, workspace=group["id"])

        self.request("POST", "/api/returns", expected=201, cookie=owner, data={
            "instrument_id": 1, "product": "deposit", "mode": "rate", "rate": "0.25", "principal": "100",
            "name": "Savings interest", "start_on": "2026-01-01",
        })
        combined, _ = self.request("GET", "/api/dashboard", cookie=jamie, workspace=group["id"])
        inherited = combined["accounts"][0]
        self.assertEqual((inherited["name"], inherited["profile_id"]), ("Legacy savings · Personal", 1))
        returns, _ = self.request("GET", "/api/returns", cookie=jamie, workspace=group["id"])
        self.assertEqual(returns["returns"][0]["amount"], "25")
        self.request("POST", "/api/transactions", expected=403, cookie=jamie, workspace=group["id"],
                     data={"account_id": inherited["id"], "kind": "income", "amount": "500",
                           "name": "Unauthorized entry", "posted_on": date.today().isoformat()})

        self.request("PUT", f"/api/workspaces/{group['id']}", cookie=owner,
                     data={"kind": "group", "name": "Family", "members": [1, third["id"]]})
        self.request("GET", "/api/returns", expected=403, cookie=jamie, workspace=group["id"])
        self.request("GET", "/api/dashboard", cookie=jamie)
        self.request("POST", "/api/auth/logout", cookie=jamie, data={})
        self.request("GET", "/api/dashboard", expected=401, cookie=jamie)
        _, headers = self.request("POST", "/api/auth/login", data={"username": "jamie", "password": "🔑"})
        jamie = headers["Set-Cookie"].split(";", 1)[0]
        roster, _ = self.request("GET", "/api/auth/members", cookie=owner)
        key = next(user["id"] for user in roster["users"] if user["profile_id"] == member["id"])
        self.request("DELETE", f"/api/auth/members/{key}/sessions", cookie=owner)
        self.request("GET", "/api/dashboard", expected=401, cookie=jamie)

    def test_proxy_requires_the_configured_origin_and_makes_the_session_cookie_secure(self):
        self.start("https://finance.example.test")
        data = {"username": "owner", "password": "?", "setup_code": SETUP_CODE}
        self.request("POST", "/api/auth/setup", data=data, expected=403,
                     headers={"Origin": "http://finance.example.test", "X-Forwarded-Proto": "https"})
        self.request("POST", "/api/auth/setup", data=data, expected=403,
                     headers={"Sec-Fetch-Site": "cross-site"})
        self.request("GET", "/", expected=403, headers={"Host": "foreign.example.test"})
        _, headers = self.request("POST", "/api/auth/setup", data=data)
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith("__Host-qfp_session="))
        self.assertIn("; Secure", cookie)
        self.assertIn("max-age=", headers["Strict-Transport-Security"])
        self.request("GET", "/api/dashboard", cookie=cookie.split(";", 1)[0])


if __name__ == "__main__":
    unittest.main()
