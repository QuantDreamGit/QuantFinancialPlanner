import json
import tempfile
import unittest
from datetime import date, timedelta
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.web.__main__ import DashboardHandler
from apps.web.cars import group_cost_report
from apps.web.storage import Database
from apps.web.values import ApiError
from apps.web.workspaces import REGISTRY_SCHEMA, WorkspaceRegistry


TODAY = date(2026, 11, 23)


class CarsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Database(Path(directory.name) / "finance.sqlite3")
        self.database.initialize()
        clock = patch("apps.web.__main__.date")
        self.addCleanup(clock.stop)
        clock.start().today.return_value = TODAY
        self.car_id = self.add_car("Panda", "4wd", ["turbo", "spare"])
        self.path = f"/api/cars/{self.car_id}"

    def api(self, method, path, data=None, *, status=200):
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.command = method
        handler.server = SimpleNamespace(database=self.database, projection=None)
        handler.headers = Message()
        if data is not None:
            body = json.dumps(data).encode()
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = str(len(body))
            handler.rfile = BytesIO(body)
        payload, actual = handler.api(path, {})
        self.assertEqual(actual, status)
        return payload

    def add_car(self, name, drivetrain, equipment=()):
        return self.api("POST", "/api/cars", {
            "name": name, "drivetrain": drivetrain, "equipment": list(equipment),
        }, status=201)["id"]

    def activity(self, kind, days_ago, km, **data):
        return self.api("POST", self.path + "/activities", {
            "kind": kind, "recorded_on": (TODAY - timedelta(days=days_ago)).isoformat(),
            "odometer_km": km, **data,
        }, status=201)["id"]

    def schedule(self, name, interval, baseline, estimate=""):
        return self.api("POST", self.path + "/schedules", {
            "name": name, "interval_km": interval, "baseline_km": baseline, "estimate": estimate,
        }, status=201)["id"]

    def test_fuel_projection_uses_full_tank_intervals_and_weighted_price(self):
        self.activity("fuel", 40, 100000, litres="50", cost="90.00", full_tank=True)
        self.activity("fuel", 32, 100200, litres="10", cost="18.00")
        self.activity("fuel", 20, 100500, litres="35", cost="63.00", full_tank=True)
        self.activity("fuel", 17, 100600, litres="5", cost="9.00")
        self.activity("odometer", 0, 101000)
        car = self.api("GET", self.path)
        self.assertEqual(car["fuel"], {
            "cost": "18000", "litres": 100, "consumption": 9, "price_per_litre": "180",
            "cost_per_km": "16", "measured_km": 500,
        })
        self.assertEqual(car["driving"]["km_in_year"], 9125)
        self.assertEqual(car["projection"]["fuel"], "147825")

    def test_services_project_dates_and_recur_after_actual_odometer(self):
        self.activity("odometer", 40, 100000)
        self.activity("odometer", 0, 101000)
        oil = self.schedule("Oil change", 20000, 90000, "140.00")
        inspections = self.schedule("Inspection", 4000, 101000)
        self.activity("service", 1, 100980, cost="60", schedule_id=inspections)
        report = self.api("GET", self.path)
        by_id = {row["id"]: row for row in report["schedules"]}
        self.assertEqual(by_id[oil]["due_km"], 110000)
        self.assertEqual(by_id[oil]["due_on"], (TODAY + timedelta(days=360)).isoformat())
        self.assertEqual(by_id[inspections]["count_in_year"], 2)
        self.assertEqual(report["projection"]["scheduled"], "26000")

        entry = self.activity("service", 0, 101000, cost="137.50", schedule_id=oil)
        self.activity("maintenance", 0, 101003, name="Wheel alignment", cost="50")
        self.activity("repair", 0, 101003, name="Door repair", cost="275")
        report = self.api("GET", self.path)
        oil_row = next(row for row in report["schedules"] if row["id"] == oil)
        self.assertEqual(oil_row["last_service_on"], TODAY.isoformat())
        self.assertEqual(oil_row["due_km"], 121000)
        self.assertEqual(oil_row["count_in_year"], 0)
        self.assertEqual(report["paid"], {"fuel": "0", "repair": "27500", "maintenance": "5000", "service": "19750"})
        self.api("DELETE", self.path + f"/schedules/{oil}")
        report = self.api("GET", self.path)
        old_service = next(row for row in report["activity"] if row["id"] == entry)
        self.assertEqual((old_service["name"], old_service["schedule_id"]), ("Oil change", None))

    def test_overdue_service_can_be_budgeted_without_a_driving_rate(self):
        self.activity("odometer", 0, 100001)
        self.schedule("Oil change", 20000, 80000, "140")
        self.schedule("Air filter", 20000, 90000, "30")
        report = self.api("GET", self.path)
        self.assertEqual(report["schedules"][0]["remaining_km"], -1)
        self.assertEqual(report["schedules"][0]["count_in_year"], 1)
        self.assertEqual(report["projection"]["scheduled"], "14000")
        self.assertEqual(report["projection"]["undated_schedules"], 1)

    def test_group_costs_break_down_paid_and_upcoming_costs_with_partial_forecasts(self):
        self.activity("fuel", 40, 100000, litres="50", cost="90", full_tank=True)
        self.activity("fuel", 32, 100200, litres="10", cost="18")
        self.activity("fuel", 20, 100500, litres="35", cost="63", full_tank=True)
        self.activity("fuel", 17, 100600, litres="5", cost="9")
        oil = self.schedule("Oil change", 4000, 96980)
        self.activity("service", 1, 100980, cost="60", schedule_id=oil)
        self.activity("maintenance", 0, 101000, name="Alignment", cost="45")
        self.activity("repair", 0, 101000, name="Door repair", cost="75")
        self.api("PUT", self.path + "/parts/front_bumper", {"condition": "repair", "estimate": "350"})
        self.api("PUT", self.path + "/parts/headlights", {"condition": "watch"})
        panda = self.api("GET", self.path)["car"]

        other = self.add_car("Golf", "fwd")
        self.path = f"/api/cars/{other}"
        self.activity("fuel", 0, 50005, litres="10", cost="20", full_tank=True)
        self.activity("maintenance", 0, 50005, name="Seasonal check", cost="30")
        self.schedule("Lamp check", 1000, 49000)
        self.schedule("Timing belt", 20000, 50000, "400")
        golf = self.api("GET", self.path)["car"]

        with self.database.connection(snapshot=True) as conn:
            costs = group_cost_report(conn, [panda, golf], TODAY)
        self.assertEqual(costs["actual"], {"fuel": "20000", "maintenance": "13500", "repair": "7500", "total": "41000"})
        self.assertEqual([item["actual"]["total"] for item in costs["cars"]], ["36000", "5000"])
        self.assertEqual(costs["forecast"], {
            "fuel": "147825", "scheduled": "12000", "planned": "159825", "health_estimates": "35000",
        })
        self.assertIsNone(costs["cars"][1]["forecast"]["fuel"])
        self.assertIsNone(costs["cars"][1]["forecast"]["scheduled"])
        self.assertEqual(costs["coverage"], {
            "missing_fuel": 1, "undated_schedules": 1, "unpriced_services": 1, "unpriced_parts": 1,
        })
        self.assertEqual([(item["name"], item["count_in_year"]) for item in costs["upcoming"]],
                         [("Lamp check", 1), ("Oil change", 2), ("Timing belt", None)])

    def test_odometer_import_is_atomic_idempotent_and_checks_existing_entries(self):
        self.activity("odometer", 0, 103000)
        path = self.path + "/odometer-import"
        data = {"csv": "\ufeffdate;odometer_km\n2026-11-03;102000\n2026-10-10;101000\n2026-11-23;103000\n"}
        self.assertEqual(self.api("POST", path, data), {"imported": 2, "duplicates": 1})
        self.assertEqual(self.api("POST", path, data), {"imported": 0, "duplicates": 3})
        with self.assertRaisesRegex(ApiError, "conflict"):
            self.api("POST", path, {"csv": "date,odometer_km\n2026-10-01,99000\n2026-11-13,100000\n"})
        with self.assertRaisesRegex(ApiError, "earlier readings"):
            self.activity("odometer", 1, 101999)
        report = self.api("GET", self.path)
        self.assertEqual([row["odometer_km"] for row in report["activity"]], [103000, 102000, 101000])

    def test_health_costs_and_markers_follow_drivetrain_and_optional_equipment(self):
        self.api("PUT", self.path + "/parts/transfer", {
            "condition": "repair", "estimate": "350.00", "note": "Workshop quote",
        })
        self.api("PUT", self.path + "/parts/headlights", {"condition": "watch"})
        self.api("PUT", self.path + "/parts/engine", {"condition": "good"})
        report = self.api("GET", self.path)
        self.assertEqual(report["projection"]["health_estimates"], "35000")
        self.assertEqual(report["projection"]["unpriced_parts"], 1)
        self.assertTrue({"front_diff", "rear_diff", "transfer", "turbo", "spare"}
                        <= {part["id"] for part in report["parts"]})

        self.api("PUT", self.path, {"name": "Panda", "drivetrain": "fwd"})
        report = self.api("GET", self.path)
        self.assertIsNone(report["projection"]["health_estimates"])
        self.assertTrue({"rear_diff", "transfer", "turbo", "spare"}.isdisjoint(
            {part["id"] for part in report["parts"]},
        ))
        with self.assertRaisesRegex(ApiError, "unavailable"):
            self.api("PUT", self.path + "/parts/transfer", {"condition": "good"})

        self.api("PUT", self.path, {"name": "Panda", "drivetrain": "4wd"})
        report = self.api("GET", self.path)
        transfer = next(part for part in report["parts"] if part["id"] == "transfer")
        self.assertEqual((transfer["condition"], transfer["note"], transfer["estimate"]), ("repair", "Workshop quote", "35000"))

    def test_records_are_car_scoped_and_tracked_outside_the_transaction_ledger(self):
        oil = self.schedule("Oil change", 20000, 80000)
        other = f"/api/cars/{self.add_car('Golf', 'rwd')}"
        with self.assertRaisesRegex(ApiError, "no longer exists"):
            self.api("POST", other + "/activities", {
                "kind": "service", "recorded_on": TODAY.isoformat(), "odometer_km": 100000, "schedule_id": oil,
            }, status=201)
        self.activity("repair", 0, 100000, name="Brake pad replacement", cost="250")
        self.assertEqual(self.database.ledger().entries, [])
        self.api("DELETE", self.path)
        self.assertEqual(len(self.api("GET", "/api/cars")["cars"]), 1)
        with self.database.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM car_activity").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM car_schedules").fetchone()[0], 0)

    def test_workspace_catalog_migrates_legacy_cars_and_links_once(self):
        oil = self.schedule("Oil change", 20000, 80000)
        self.activity("service", 0, 100000, schedule_id=oil, cost="100")
        self.api("PUT", self.path + "/parts/headlights", {"condition": "watch", "estimate": "55"})
        folder = self.database.path.with_suffix(".workspaces")
        folder.mkdir()
        with Database(folder / "registry.sqlite3").connection() as conn:
            conn.executescript(REGISTRY_SCHEMA)
            conn.execute("INSERT INTO workspaces VALUES (2, 'Family', 'group')")
        car_db = Database(folder / "2.sqlite3")
        car_db.initialize()
        with car_db.connection(write=True) as conn:
            conn.execute("INSERT INTO cars (id, name, drivetrain) VALUES (1, 'Van', 'rwd')")
        WorkspaceRegistry(self.database)
        registry = WorkspaceRegistry(self.database)
        with registry.connection() as conn:
            cars = {row["name"]: (row["id"], row["workspace_id"]) for row in conn.execute("""
                SELECT cars.*, car_assignments.workspace_id FROM cars JOIN car_assignments ON car_assignments.car_id = cars.id
            """)}
            self.assertEqual(set(cars), {"Panda", "Van"})
            panda, van = cars["Panda"], cars["Van"]
            self.assertEqual((panda[1], van[1]), (1, 2))
            self.assertNotEqual(panda[0], van[0])
            service = conn.execute("""
                SELECT car_activity.cost, car_schedules.name, car_schedules.car_id FROM car_activity
                JOIN car_schedules ON car_schedules.id = car_activity.schedule_id WHERE car_activity.car_id = ?
            """, (panda[0],)).fetchone()
            self.assertEqual(tuple(service), (10000, "Oil change", panda[0]))
            inspection = conn.execute("SELECT estimate FROM car_inspections WHERE car_id = ?", (panda[0],)).fetchone()
            self.assertEqual(inspection["estimate"], 5500)
