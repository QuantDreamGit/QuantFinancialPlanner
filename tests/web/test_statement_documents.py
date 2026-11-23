import base64
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch

from apps.web.__main__ import DashboardServer
from apps.web.attachments import Download
from apps.web.statement_documents import (
    MAX_FILENAME_LENGTH,
    MAX_STATEMENT_PDF_BYTES,
    archive_statement_pdf,
    handle_statement_documents,
    initialize_statement_documents,
)
from apps.web.storage import Database
from apps.web.values import ApiError
from apps.web.workspaces import WorkspaceRegistry, group_id


PDF = b"%PDF-1.7\n% Fictional test bank statement\n%%EOF\n"
TODAY = date(2026, 11, 21)
SETUP_CODE = "temporary-archive-test-token-0123456789"
ROUTE = "/api/imports/documents"


def create_account(database, name, kind="checking"):
    return database.save_account({"name": name, "kind": kind, "opening_balance": "100",
                                  "opened_on": "2026-01-01"}, TODAY)


class StatementDocumentsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Database(Path(folder.name) / "ledger.sqlite3")
        self.database.initialize()
        self.account = create_account(self.database, "Main account")

    def archive(self, filename="statement.pdf", label="November statement", content=PDF, *,
                account_id=None, database=None, today=TODAY):
        return archive_statement_pdf(database or self.database, self.account if account_id is None else account_id,
                                     filename, label, content, today)

    def request(self, method="GET", key="", query=None, database=None):
        handler = SimpleNamespace(database=database or self.database)
        return handle_statement_documents(handler, ROUTE + key, query or {}, method, TODAY)

    def assert_status(self, status, call, field=None):
        with self.assertRaises(ApiError) as error:
            call()
        self.assertEqual(error.exception.status, status)
        if field:
            self.assertEqual(error.exception.field, field)

    def test_list_download_and_delete_survive_reopening_the_ledger(self):
        item = self.archive("Bank café.PDF")
        self.assertEqual(item, {
            "id": 1, "account_id": self.account, "filename": "Bank café.PDF", "name": "November statement",
            "size": len(PDF), "archived_on": TODAY.isoformat(), "download_url": f"{ROUTE}/1",
        })
        self.database.save_entry({"kind": "expense", "account_id": self.account, "name": "Posted expense",
                                  "amount": "12.34", "posted_on": TODAY.isoformat()}, TODAY)
        ledger = self.database.ledger()
        reopened = Database(self.database.path)
        initialize_statement_documents(reopened)
        self.assertEqual(self.request(database=reopened), ({"documents": [item], "total": 1, "page": 1, "size": 25}, 200))
        self.assertEqual(self.request(key="/1", database=reopened), (Download(body=PDF, name=item["filename"]), 200))
        self.assertEqual(self.request("DELETE", "/1", database=reopened), ({"id": 1}, 200))
        self.assert_status(404, lambda: self.request(key="/1", database=reopened))
        self.assert_status(404, lambda: self.request("DELETE", "/1", database=reopened))
        self.assertEqual(reopened.ledger(), ledger)

    def test_digest_is_per_account_and_reuploads_keep_the_original_metadata(self):
        first = self.archive("First upload.pdf")
        retry = self.archive("Renamed.pdf", "Different label", today=date(2026, 12, 1))
        self.assertEqual(first, retry)
        other = create_account(self.database, "Other account", "savings")
        second = self.archive("From savings.pdf", account_id=other)
        self.assertNotEqual(first["id"], second["id"])
        third = self.archive("First upload.pdf", content=PDF + b"% revised")
        self.assertNotEqual(first["id"], third["id"])
        self.assertEqual(self.request(query={"account_id": str(other)})[0]["documents"], [second])
        self.assertEqual(self.request(query={"page": "2", "size": "1"})[0], {
            "documents": [second], "total": 3, "page": 2, "size": 1,
        })

    def test_concurrent_pdf_uploads_share_one_original(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.archive(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.request()[0]["total"], 1)

    def test_filename_is_safe_and_bounded_without_changing_the_raw_pdf(self):
        safe = self.archive("C:\\fakepath\\nested/Bar\r\n\u202ecafe\u0301.PDF")
        self.assertEqual(safe["filename"], "Barcafé.PDF")
        long_name = self.archive("T" * 400 + ".pdf", content=PDF + b"% second")
        self.assertEqual(len(long_name["filename"]), MAX_FILENAME_LENGTH)
        self.assertTrue(long_name["filename"].endswith(".pdf"))
        default = self.archive(None, content=PDF + b"% third")
        self.assertEqual(default["filename"], "Statement.pdf")
        self.assertEqual(self.request(key="/1")[0].body, PDF)

    def test_invalid_inputs_and_limits_leave_the_archive_empty(self):
        self.assert_status(422, lambda: self.archive(content=b"not a PDF"), "file")
        self.assert_status(422, lambda: self.archive(content=None), "file")
        self.assert_status(422, lambda: self.archive(label="x" * 101), "name")
        self.assert_status(413, lambda: self.archive(content=PDF + b"x" * MAX_STATEMENT_PDF_BYTES), "file")
        self.assert_status(422, lambda: self.archive(account_id=999), "account_id")
        investment = create_account(self.database, "Investments", "investment")
        self.assert_status(422, lambda: self.archive(account_id=investment), "account_id")
        for query in ({"page": "0"}, {"page": "1000001"}, {"size": "51"}, {"size": "NaN"}, {"account_id": ""}):
            with self.subTest(query=query):
                self.assert_status(422, lambda: self.request(query=query))
        self.assert_status(405, lambda: self.request("POST"))
        self.assertEqual(self.request()[0]["total"], 0)

    def test_family_and_personal_archives_are_separate_and_only_local_accounts_are_eligible(self):
        registry = WorkspaceRegistry(self.database)
        member = registry.save({"kind": "profile", "name": "Partner"})
        group = registry.save({"kind": "group", "name": "Family", "members": [1, member["id"]]})
        personal = self.archive("Personal.pdf")
        combined = registry.database(group)
        inherited = group_id(1, self.account)
        self.assertIn(inherited, [row["id"] for row in combined.ledger().accounts])
        self.assertEqual(self.request(database=combined)[0]["documents"], [])
        self.assert_status(404, lambda: self.request(key=f"/{personal['id']}", database=combined))
        self.assert_status(422, lambda: self.archive(account_id=inherited, database=combined), "account_id")
        joint = create_account(combined, "Joint cash", "cash")
        family = self.archive("Family.pdf", account_id=joint, database=combined)
        self.assertEqual(self.request(database=combined)[0]["documents"], [family])
        self.assertEqual(self.request()[0]["documents"], [personal])

    def test_account_cannot_be_deleted_while_the_original_references_it(self):
        item = self.archive()
        self.assert_status(409, lambda: self.database.delete("accounts", self.account))
        self.request("DELETE", f"/{item['id']}")
        self.database.delete("accounts", self.account)
        self.assertEqual(self.database.ledger().accounts, [])

    def test_authenticated_http_download_is_private_and_delivered_as_an_attachment(self):
        item = self.archive("Bank café.pdf")
        server = DashboardServer(0, self.database, None, setup_code=SETUP_CODE)
        self.addCleanup(server.server_close)
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)

        def request(method, path, *, cookie=None, data=None, workspace=None):
            headers = {"Host": f"127.0.0.1:{server.server_port}"}
            if cookie:
                headers["Cookie"] = cookie
            if data is not None:
                headers["Content-Type"] = "application/json"
            if workspace:
                headers["X-Workspace"] = str(workspace)
            link = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                link.request(method, path, json.dumps(data) if data is not None else None, headers)
                response = link.getresponse()
                return response.status, response.headers, response.read()
            finally:
                link.close()

        self.assertEqual(request("GET", item["download_url"])[0], 401)
        status, headers, _ = request("POST", "/api/auth/setup", data={
            "username": "owner", "password": "test passphrase", "setup_code": SETUP_CODE,
        })
        self.assertEqual(status, 200)
        owner = headers["Set-Cookie"].split(";", 1)[0]
        status, headers, body = request("GET", item["download_url"], cookie=owner)
        self.assertEqual((status, body), (200, PDF))
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("filename*=UTF-8''Bank%20caf%C3%A9.pdf", headers["Content-Disposition"])
        self.assertEqual(request("GET", ROUTE, cookie=owner)[0], 200)

        raw = PDF.replace(b"Fictional", b"Second fictional")
        payload = {"source": "pdf", "file": base64.b64encode(raw).decode("ascii"), "account_id": self.account,
                   "filename": "original upload.pdf", "name": "Coffee statement"}
        extracted = f"{date.today().isoformat()} Coffee -2.50"
        with patch("apps.web.imports._pdf_text", return_value=extracted) as local:
            status, _, body = request("POST", "/api/imports/preview", cookie=owner, data=payload)
        self.assertEqual(status, 200)
        local.assert_called_once_with(raw, ocr=False)
        preview = json.loads(body)
        self.assertEqual(len(preview["rows"]), 1)
        archived = preview["document"]
        self.assertEqual((archived["filename"], archived["name"]), (payload["filename"], payload["name"]))
        status, _, body = request("GET", archived["download_url"], cookie=owner)
        self.assertEqual((status, body), (200, raw))
        self.assertEqual(self.database.ledger().entries, [])

        ai_row = {"date": date.today().isoformat(), "description": "Coffee", "amount": "-2.50",
                  "currency": "EUR", "reference": ""}
        with (patch("apps.web.imports._pdf_text", return_value=extracted),
              patch("apps.web.imports.extract_statement", return_value={"rows": [ai_row], "notices": []}) as ai):
            status, _, body = request("POST", "/api/imports/preview", cookie=owner, data=dict(
                payload, pdf_method="ai", ai_consent=True,
            ))
        self.assertEqual(status, 200)
        self.assertEqual(ai.call_args.args, (extracted,))
        self.assertEqual(json.loads(body)["document"], archived)

        rejected = dict(payload, file=base64.b64encode(raw + b"% rejected").decode("ascii"))
        with patch("apps.web.imports._pdf_text", side_effect=ApiError("Extraction could not finish.", "file")):
            self.assertEqual(request("POST", "/api/imports/preview", cookie=owner, data=rejected)[0], 422)
        self.assertEqual(json.loads(request("GET", ROUTE, cookie=owner)[2])["total"], 2)

        status, _, body = request("POST", "/api/workspaces", cookie=owner, data={"name": "Member", "kind": "profile"})
        self.assertEqual(status, 201)
        self.assertEqual(request("GET", item["download_url"], cookie=owner, workspace=json.loads(body)["id"])[0], 403)
        self.assertEqual(request("DELETE", item["download_url"], cookie=owner)[0], 200)
        self.assertEqual(request("GET", item["download_url"], cookie=owner)[0], 404)
