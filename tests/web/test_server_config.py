import argparse
import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch

from apps.web.backups import create_backup, ledger_access
from apps.web.settings import BackupSettings, add_settings_arguments, load_settings, network_binding


class ServerConfigTests(unittest.TestCase):
    def test_custom_config_paths_and_command_line_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            config = base / "server.ini"
            config.write_text(
                "[server]\nmode = netbird\nport = 8443\ndatabase = files/main.db\nbuild = false\n"
                "projection_bin = out/planner_projection\n"
                "[backups]\nenabled = yes\ndirectory = copies\ninterval_hours = 12\nkeep = 4\n",
                encoding="utf-8",
            )
            parser = argparse.ArgumentParser()
            add_settings_arguments(parser)
            settings = load_settings(parser.parse_args(["--config", str(config)]))
            self.assertEqual((settings.mode, settings.port, settings.build), ("netbird", 8443, False))
            self.assertEqual(settings.database, base / "files/main.db")
            self.assertEqual(settings.projection_bin, base / "out/planner_projection")
            self.assertEqual(settings.backups, BackupSettings(True, base / "copies", 12, 4))
            altered = load_settings(parser.parse_args(["--config", str(config), "--lan", "--port", "0"]))
            self.assertEqual((altered.mode, altered.port), ("lan", 0))

    def test_overlay_modes_keep_the_provider_address_and_limit_peers(self):
        with patch("apps.web.settings.command", return_value="100.87.1.12\n") as command:
            tailscale = network_binding("tailscale")
        command.assert_called_once_with("tailscale", "ip", "-4")
        self.assertEqual(tailscale.host, "100.87.1.12")
        self.assertTrue(tailscale.allows("100.88.27.2"))
        self.assertFalse(tailscale.allows("192.168.1.42"))

        with patch("apps.web.settings.command", return_value="Daemon version: dev\nNetBird IP: 100.88.27.2/16\n"):
            netbird = network_binding("netbird")
        self.assertEqual(netbird.host, "100.88.27.2")
        self.assertTrue(netbird.allows("100.88.20.1"))
        self.assertFalse(netbird.allows("100.89.20.1"))
        with patch("apps.web.settings.command", return_value="NetBird IP: N/A\n"):
            with self.assertRaisesRegex(ValueError, "connected VPN client"):
                network_binding("netbird")


class BackupTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.database = self.root / "data" / "main.sqlite3"
        self.workspace = self.database.with_suffix(".workspaces")
        self.workspace.mkdir(parents=True)
        self.options = BackupSettings(True, self.root / "backups", 24, 2)

    @staticmethod
    def write(path, value):
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS ledger (value TEXT)")
            connection.execute("INSERT INTO ledger VALUES (?)", (value,))
            connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise

    def test_backup_contains_live_wal_state_workspaces_and_key_and_keeps_recent_snapshots(self):
        connection = self.write(self.database, "primary")
        self.addCleanup(connection.close)
        self.assertTrue(self.database.with_name(self.database.name + "-wal").exists())
        for name, value in (("registry.sqlite3", "registry"), ("2.sqlite3", "profile")):
            self.write(self.workspace / name, value).close()
        (self.workspace / "openai-fernet.key").write_bytes(b"synthetic test key")

        first = create_backup(self.database, self.options, due_only=True)
        self.assertIsNone(create_backup(self.database, self.options, due_only=True))
        manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["files"]), 4)
        for item in manifest["files"]:
            file = first / item["path"]
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(file.stat().st_size, item["bytes"])
            self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), item["sha256"])
        restored = first / "ledger"
        for file, value in ((restored / self.database.name, "primary"),
                            (restored / self.workspace.name / "2.sqlite3", "profile")):
            with closing(sqlite3.connect(file)) as clone:
                self.assertEqual(clone.execute("SELECT value FROM ledger").fetchone(), (value,))
        self.assertEqual((restored / self.workspace.name / "openai-fernet.key").read_bytes(), b"synthetic test key")
        unrelated = self.options.directory / "quant-20000101T000000000000Z"
        unrelated.mkdir()
        (unrelated / "manifest.json").write_text('{"format":1,"source":"other ledger"}', encoding="utf-8")
        second = create_backup(self.database, self.options)
        third = create_backup(self.database, self.options)
        self.assertEqual(set(self.options.directory.iterdir()), {second, third, unrelated})

    def test_backup_waits_for_in_flight_ledger_work(self):
        self.write(self.database, "before").close()
        started = Event()

        def snapshot():
            started.set()
            return create_backup(self.database, self.options)

        with ThreadPoolExecutor(max_workers=1) as executor:
            with ledger_access(self.database):
                pending = executor.submit(snapshot)
                self.assertTrue(started.wait(timeout=2))
                self.assertFalse(pending.done())
                self.write(self.database, "during").close()
            folder = pending.result(timeout=5)
        with closing(sqlite3.connect(folder / "ledger" / self.database.name)) as copy:
            self.assertEqual(copy.execute("SELECT value FROM ledger ORDER BY rowid").fetchall(),
                             [("before",), ("during",)])
