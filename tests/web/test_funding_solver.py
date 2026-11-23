import itertools
import random
import unittest

from apps.web.funding_solver import earliest_without_transfer, solve_funding


def route(identifier, source, arrivals, destination=1):
    return {"id": identifier, "source_id": source, "destination_id": destination, "arrivals": arrivals}


def check_plan(balances, floors, routes, payer, result):
    """Verify the entire transfer schedule directly against approved event days."""
    indexed = {item["id"]: item for item in routes}
    donor_ids = set()
    adjusted = {account: list(series) for account, series in balances.items()}
    for transfer in result["transfers"]:
        source, departure, arrival = transfer["source_id"], transfer["departure"], transfer["arrival"]
        amount = transfer["amount"]
        legal = indexed[transfer["route_id"]]
        assert amount > 0 and amount == int(amount)
        assert legal["source_id"] == source and legal["destination_id"] == transfer["destination_id"] == payer
        assert legal["arrivals"][departure] == arrival
        donor_ids.add(source)
        for day in range(departure, len(adjusted[source])):
            adjusted[source][day] -= amount
        for day in range(arrival, len(adjusted[payer])):
            adjusted[payer][day] += amount
    assert sum(item["amount"] for item in result["transfers"]) == result["moved"]
    for account in donor_ids | {payer}:
        assert all(value >= floor for value, floor in zip(adjusted[account], floors[account]))
    assert adjusted[payer] == result["balances"]


def smallest_by_exhaustion(balances, floors, routes, payer):
    days = len(balances[payer])
    edges = [(item["source_id"], departure, arrival)
             for item in routes if item["destination_id"] == payer and item["source_id"] != payer
             for departure, arrival in item["arrivals"].items()]
    limits = [min(3, max(0, balances[source][departure] - floors[source][departure]))
              for source, departure, _ in edges]
    best = None
    for amounts in itertools.product(*(range(limit + 1) for limit in limits)):
        moved = sum(amounts)
        if best is not None and moved >= best:
            continue
        used = {source for (source, _, _), amount in zip(edges, amounts) if amount}
        # Exhaustively recompute every ending balance instead of using solver's capacity formula.
        if any(
            balances[source][day] - sum(
                amount for (sender, departure, _), amount in zip(edges, amounts) if sender == source and departure <= day
            ) < floors[source][day]
            for source in used for day in range(days)
        ):
            continue
        if all(
            balances[payer][day] + sum(amount for (_, _, arrival), amount in zip(edges, amounts) if arrival <= day)
            >= floors[payer][day] for day in range(days)
        ):
            best = moved
    return best


class FundingTests(unittest.TestCase):
    def test_minimal_top_ups_respect_floor_increase_after_departure(self):
        balances = {1: [200, 150, 450, 100], 2: [700, 700, 650, 610], 3: [400, 600, 950, 750]}
        floors = {1: [200] * 4, 2: [400, 400, 600, 600], 3: [400, 400, 700, 700]}
        routes = [route(10, 2, {0: 1, 1: 2, 2: 3}), route(20, 3, {0: 0, 1: 1, 2: 2, 3: 3})]
        blocked = solve_funding(balances, floors, routes, 1)
        self.assertFalse(blocked["feasible"])
        self.assertEqual(blocked["transfers"], [])
        self.assertEqual(blocked["balances"], balances[1])

        balances[3][-1] = 830
        result = solve_funding(balances, floors, routes, 1)
        self.assertTrue(result["feasible"])
        self.assertEqual(result["moved"], 100)
        self.assertEqual(result["transfers"], [
            {"route_id": 20, "source_id": 3, "destination_id": 1, "departure": 1, "arrival": 1, "amount": 50},
            {"route_id": 20, "source_id": 3, "destination_id": 1, "departure": 3, "arrival": 3, "amount": 50},
        ])
        check_plan(balances, floors, routes, 1, result)

    def test_sender_cannot_reuse_headroom_on_another_route(self):
        balances = {1: [0, -800, -1600], 2: [1000, 1000, 1000]}
        floors = {key: [0] * 3 for key in balances}
        routes = [route(1, 2, {0: 1}), route(2, 2, {2: 2})]
        result = solve_funding(balances, floors, routes, 1)
        self.assertFalse(result["feasible"])
        self.assertEqual((result["moved"], result["transfers"]), (0, []))

    def test_only_use_donors_with_original_reserves_intact_and_approved_direct_routes(self):
        balances = {1: [-10, -10, -10], 2: [-1, 100, 100], 3: [400, 400, 400], 4: [500, 500, 500]}
        floors = {key: [0] * 3 for key in balances}
        routes = [route(1, 2, {1: 1}), route(2, 3, {0: 1, 1: 2}), route(3, 4, {0: 0}, destination=3)]
        result = solve_funding(balances, floors, routes, 1)
        self.assertFalse(result["feasible"])
        balances[1][0] = 0
        result = solve_funding(balances, floors, routes, 1)
        self.assertTrue(result["feasible"])
        self.assertEqual([item["source_id"] for item in result["transfers"]], [3])
        check_plan(balances, floors, routes, 1, result)

    def test_prefers_latest_approved_departure_arriving_by_the_deadline(self):
        balances = {1: [0, 10, -70, -90], 2: [50, 50, 100, 80], 3: [40, 40, 40, 40]}
        floors = {key: [0] * 4 for key in balances}
        routes = [route(1, 2, {0: 2, 1: 3}), route(2, 2, {2: 2}), route(3, 3, {2: 3})]
        result = solve_funding(balances, floors, routes, 1)
        self.assertEqual(result["moved"], 90)
        self.assertEqual(result["transfers"][0],
                         {"route_id": 2, "source_id": 2, "destination_id": 1, "departure": 2, "arrival": 2, "amount": 70})
        check_plan(balances, floors, routes, 1, result)

    def test_no_need_returns_no_action_and_large_cents_remain_exact(self):
        large = 4_000_000_000_000_000_003
        balances = {1: [100, 100], 2: [large, large]}
        floors = {1: [100, 99], 2: [10, 10]}
        routes = [route(1, 2, {0: 0})]
        self.assertEqual(solve_funding(balances, floors, routes, 1),
                         {"feasible": True, "moved": 0, "transfers": [], "balances": [100, 100]})
        balances[1][1] = -(large - 111)
        result = solve_funding(balances, floors, routes, 1)
        self.assertEqual(result["moved"], large - 12)
        check_plan(balances, floors, routes, 1, result)

    def test_earliest_purchase_uses_future_minimum_and_validates_whole_horizon(self):
        self.assertEqual(earliest_without_transfer([200, 700, 400, 800], [100] * 4, 500, 0), 3)
        self.assertEqual(earliest_without_transfer([500, 600, 800], [100] * 3, 400, 1), 1)
        self.assertIsNone(earliest_without_transfer([-1, 600, 800], [0] * 3, 10, 1))
        self.assertIsNone(earliest_without_transfer([800, 900, 200], [100] * 3, 200, 0))
        self.assertIsNone(earliest_without_transfer([], [], 0, 0))
        self.assertIsNone(earliest_without_transfer([500], [0], 1, 1))

    def test_malformed_balance_and_route_inputs_rejected(self):
        with self.assertRaises(ValueError):
            solve_funding({1: [-1]}, {}, [], 1)
        with self.assertRaises(ValueError):
            solve_funding({1: [-1], 2: [10, 12]}, {1: [0], 2: [0, 0]}, [], 1)
        with self.assertRaises(ValueError):
            solve_funding({1: [-1], 2: [10]}, {1: [0], 2: [0]}, [route(1, 2, {0: -1})], 1)
        with self.assertRaises(ValueError):
            earliest_without_transfer([100], [0], 1.5, 0)

    def test_small_forecasts_match_exhaustive_search(self):
        rng = random.Random(48)
        for sample in range(100):
            days = 3
            balances = {1: [rng.randint(-3, 4) for _ in range(days)],
                        2: [rng.randint(-1, 3) for _ in range(days)],
                        3: [rng.randint(-1, 3) for _ in range(days)]}
            floors = {account: [rng.randint(0, 1) for _ in range(days)] for account in balances}
            routes = [route(account, account, {day: rng.randrange(day, days) for day in range(days) if rng.random() < 0.75})
                      for account in (2, 3)]
            expected = smallest_by_exhaustion(balances, floors, routes, 1)
            result = solve_funding(balances, floors, routes, 1)
            with self.subTest(sample=sample):
                self.assertEqual(result["feasible"], expected is not None)
                if expected is not None:
                    self.assertEqual(result["moved"], expected)
                    check_plan(balances, floors, routes, 1, result)
