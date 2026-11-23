import os
import tempfile
import unittest
from pathlib import Path

from apps.web.planning import Projection
from apps.web.storage import Database
from apps.web.values import ApiError


BINARY = Path(os.environ.get("PLANNER_PROJECTION", "build/planner_projection")).resolve()


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.projection = Projection(BINARY)
        self.example = self.projection.example()

    def test_shared_example_returns_both_schedules(self):
        _, result = self.projection.compare(self.example)
        upfront, gradual = result["plans"]
        self.assertEqual(upfront["lowestAvailableCash"], "500000")
        schedule = gradual["projection"]
        self.assertEqual(len(schedule), 7)
        self.assertEqual(schedule[0]["contribution"], "100000")
        self.assertEqual(schedule[-1]["availableCash"], "1400000")

    def test_feasibility_and_exact_shortfall(self):
        _, result = self.projection.compare(dict(self.example, purchasePrice="19000"))
        self.assertEqual([plan["approach"] for plan in result["plans"]], ["gradual"])
        _, result = self.projection.compare(dict(self.example, purchasePrice="20000,01"))
        self.assertEqual(result["plans"], [])
        self.assertEqual(result["shortfall"], "100001")

    def test_fields_identified_for_invalid_inputs(self):
        for field, value in (("purchasePrice", "12.345"), ("dueMonth", "2026-10"), ("minimumReserve", "-1")):
            with self.subTest(field=field), self.assertRaises(ApiError) as failure:
                self.projection.compare(dict(self.example, **{field: value}))
            self.assertEqual(failure.exception.field, field)

    def test_large_cent_values_are_serialized_exactly(self):
        _, result = self.projection.compare(dict(
            self.example, startingCash="92233720368547758.07", purchasePrice="100.01",
            dueMonth=self.example["startingMonth"],
        ))
        self.assertEqual(result["plans"][0]["lowestAvailableCash"], "9223372036854765806")

    def test_saved_comparison_keeps_its_original_projection(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "test.sqlite3"
        db = Database(path)
        db.initialize()
        inputs, result = self.projection.compare(self.example)
        item = db.save_scenario("Next car", inputs, result)
        reopened = Database(path).scenario(item)
        self.assertEqual(reopened["name"], "Next car")
        self.assertEqual(reopened["input"], inputs)
        self.assertEqual(reopened["result"], result)
