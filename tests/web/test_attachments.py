import base64
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from apps.web.attachments import (
    MAX_ATTACHMENTS,
    MAX_FILE_BYTES,
    MAX_UPLOAD_BODY,
    Download,
    handle_attachments,
    initialize_attachments,
    list_attachments,
    save_attachment,
)
from apps.web.storage import Database
from apps.web.values import ApiError
from apps.web.workspaces import WorkspaceRegistry, group_id


PDF = b"%PDF-1.7\nFictional receipt 3729.\n%%EOF"
PNG = bytes.fromhex("89 50 4e 47 0d 0a 1a 0a") + b"mock-image"
TODAY = date(2026, 11, 21)


def wire(name, content=PDF):
    return {"name": name, "file": base64.b64encode(content).decode("ascii")}


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.database = self.open_db("personal")
        self.entry = self.new_entry(self.database)

    def open_db(self, name):
        database = Database(self.folder / (name + ".sqlite"))
        database.initialize()
        initialize_attachments(database)
        return database

    def new_entry(self, database, name="Local bank"):
        account = database.save_account({"name": name, "kind": "checking", "opening_balance": "100",
                                         "opened_on": "2026-01-01"}, TODAY)
        return database.save_entry({"kind": "expense", "account_id": account, "amount": "10",
                                    "name": "Booked purchase", "posted_on": TODAY.isoformat()}, TODAY)

    def request(self, verb, suffix="", *, data=None, entry=None, database=None):
        database = database or self.database
        entry = self.entry if entry is None else entry

        def read_body(limit):
            self.assertEqual(MAX_UPLOAD_BODY, limit)
            return data

        handler = SimpleNamespace(database=database, read_data=read_body)
        return handle_attachments(handler, f"/api/transactions/{entry}/attachments{suffix}", {}, verb, TODAY)

    def assert_status(self, code, action):
        with self.assertRaises(ApiError) as guarded:
            action()
        self.assertEqual(guarded.exception.status, code)

    def test_upload_list_download_and_delete_round_trip(self):
        filename = "Ricevuta caffè (novembre).PDF"
        item, status = self.request("POST", data=wire(filename))
        self.assertEqual(status, 201)
        self.assertEqual(item, {"id": 1, "name": filename, "size": len(PDF), "uploaded_on": TODAY.isoformat()})
        original, status = self.request("GET")
        self.assertEqual((original, status), ({"attachments": [item]}, 200))
        content, status = self.request("GET", "/1")
        self.assertEqual((content, status), (Download(body=PDF, name=filename), 200))
        self.assertEqual(self.request("DELETE", "/1"), ({"id": 1}, 200))
        self.assertEqual(self.request("GET"), ({"attachments": []}, 200))
        self.assert_status(404, lambda: self.request("GET", "/1"))

    def test_retries_are_idempotent_even_when_concurrent(self):
        def upload():
            return save_attachment(Database(self.database.path), self.entry, wire("same.pdf"), TODAY)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _run: upload(), range(2)))
        self.assertEqual(sorted((first[1], second[1])), [200, 201])
        self.assertEqual(first[0], second[0])
        later, status = self.request("POST", data=wire("same.pdf", PDF + b"copy revised"))
        self.assertEqual(status, 201)
        self.assertNotEqual(later["id"], first[0]["id"])
        self.assertEqual(len(list_attachments(self.database, self.entry)), 2)

    def test_transaction_and_database_scope_keep_documents_isolated(self):
        this_file, _ = self.request("POST", data=wire("private.pdf"))
        own_other = self.new_entry(self.database, "Another account")
        other_database = self.open_db("different-person")
        other_entry = self.new_entry(other_database)
        self.assertEqual(other_entry, self.entry)
        for entry, database in ((own_other, self.database), (other_entry, other_database)):
            self.assertEqual(self.request("GET", entry=entry, database=database), ({"attachments": []}, 200))
            for verb in ("GET", "DELETE"):
                self.assert_status(404, lambda: self.request(verb, "/" + str(this_file["id"]), entry=entry, database=database))
        remaining, _ = self.request("GET", "/" + str(this_file["id"]))
        self.assertEqual(remaining.body, PDF)

    def test_family_only_accesses_its_joint_entry_documents(self):
        registry = WorkspaceRegistry(self.database)
        partner = registry.save({"kind": "profile", "name": "Partner"})
        workspace = registry.save({"kind": "group", "name": "Family", "members": [1, partner["id"]]})
        group = registry.database(workspace)
        initialize_attachments(group)
        item, _ = self.request("POST", data=wire("solo.pdf"))
        mirror = group_id(1, self.entry)
        self.assertIn(mirror, [row["id"] for row in group.ledger().entries])
        for method, suffix in (("GET", ""), ("GET", "/" + str(item["id"])), ("POST", ""), ("DELETE", "/" + str(item["id"]))):
            self.assert_status(404, lambda: self.request(method, suffix, data=wire("solo.pdf"), entry=mirror, database=group))
        joint = self.new_entry(group, "Family checking")
        joint_item, status = self.request("POST", data=wire("joint.png", PNG), entry=joint, database=group)
        self.assertEqual(status, 201)
        self.assertEqual(self.request("GET", entry=joint, database=group), ({"attachments": [joint_item]}, 200))
        self.assertEqual(self.request("GET"), ({"attachments": [item]}, 200))

    def test_deleted_entry_cascades_files_and_unbooked_ids_are_rejected(self):
        self.request("POST", data=wire("local.pdf"))
        self.database.delete("entries", self.entry)
        self.assert_status(404, lambda: self.request("POST", data=wire("local.pdf")))
        self.assert_status(404, lambda: self.request("GET"))
        with self.database.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entry_attachments").fetchone()[0], 0)

    def test_malformed_files_and_file_size_and_count_limit(self):
        invalid_data = [
            wire("../escape.pdf"), wire("nested" + chr(92) + "file.pdf"), wire("break\r\nheader.pdf"),
            wire("2026-receipt\u202e.pdf"), wire("script.html"), wire("data.pdf", b"not a PDF"),
            {"name": "orphan.pdf", "file": "invalid@"}, {"name": "empty.pdf", "file": ""},
        ]
        for payload in invalid_data:
            with self.subTest(name=payload["name"]):
                self.assert_status(422, lambda: self.request("POST", data=payload))
        over = PDF + b"x" * (MAX_FILE_BYTES + 1 - len(PDF))
        self.assert_status(413, lambda: self.request("POST", data=wire("huge.pdf", over)))
        self.assertEqual(self.request("GET"), ({"attachments": []}, 200))
        for number in range(MAX_ATTACHMENTS):
            self.assertEqual(self.request("POST", data=wire(f"file-{number}.pdf"))[1], 201)
        self.assertEqual(self.request("POST", data=wire("file-0.pdf"))[1], 200)
        self.assert_status(409, lambda: self.request("POST", data=wire("too-many.pdf")))
