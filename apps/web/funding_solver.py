"""Exact cent funding over direct approved routes and daily account floors."""
from bisect import bisect_left
from collections import Counter, defaultdict


def _suffix_slack(balance, floor):
    if len(balance) != len(floor):
        raise ValueError("Balance and floor series must cover the same days.")
    safe = [0] * len(balance)
    for index in range(len(balance) - 1, -1, -1):
        if type(balance[index]) is not int or type(floor[index]) is not int:
            raise ValueError("Balance and floor series must use integer cents.")
        slack = balance[index] - floor[index]
        safe[index] = min(slack, safe[index + 1]) if index + 1 < len(safe) else slack
    return safe


def earliest_without_transfer(balances, floors, amount, first_index):
    """Find the first day a purchase can be paid from one account's own balance."""
    if type(amount) is not int or amount < 0:
        raise ValueError("The purchase amount must be nonnegative integer cents.")
    if type(first_index) is not int or first_index < 0:
        raise ValueError("The first day must be a nonnegative index.")
    safe = _suffix_slack(balances, floors)
    if first_index >= len(safe) or safe[0] < 0:
        return None
    date = bisect_left(safe, amount, lo=first_index)
    return date if date < len(safe) else None


def solve_funding(balances, floors, routes, payer_id):
    """Top up an end-of-day payer series using direct approved transfers.

    The payer's series may already include the proposed purchase. All other
    balances are original projections, including existing commitments. This
    solver leaves those commitments intact and never uses a donor whose
    original series breaches a floor. Only direct routes to the payer are used.

    A donor can cumulatively send at most the suffix minimum of its slack
    after a departure. For each payer deadline, its latest approved departure
    arriving by that deadline supplies the greatest eligible capacity. Those
    donor capacities only increase as deadlines advance. Allocating earlier
    top-ups therefore spends exactly the same aggregate capacity at each later
    deadline regardless of which eligible donor supplied the earlier money.
    A deficit that exceeds aggregate headroom at its deadline is infeasible.
    Successful top-ups add exactly the greatest positive payer shortfall,
    which is a lower bound on the total funding of any valid solution.
    """
    if payer_id not in balances or balances.keys() != floors.keys():
        raise ValueError("Supply a payer and a floor series for every account.")
    horizon = len(balances[payer_id])
    safe = {}
    for account, balance in balances.items():
        if len(balance) != horizon:
            raise ValueError("Every account must cover the same days.")
        safe[account] = _suffix_slack(balance, floors[account])
    original = list(balances[payer_id])
    if not horizon or safe[payer_id][0] >= 0:
        return {"feasible": True, "moved": 0, "transfers": [], "balances": original}

    arrivals = defaultdict(list)
    for route in routes:
        source = route["source_id"]
        if route["destination_id"] != payer_id or source == payer_id:
            continue
        if source not in safe:
            raise ValueError("A route references an unknown donor account.")
        if safe[source][0] < 0:
            continue
        for departure, arrival in route["arrivals"].items():
            if type(departure) is not int or type(arrival) is not int or not 0 <= departure <= arrival < horizon:
                raise ValueError("A route must depart and arrive within the forecast, in that order.")
            if safe[source][departure]:
                arrivals[arrival].append((source, departure, route["id"]))

    latest = {}
    used = Counter()
    sent = Counter()
    moved = 0
    for day, balance in enumerate(original):
        for source, departure, route in arrivals[day]:
            if source not in latest or departure > latest[source][0]:
                latest[source] = (departure, day, route)
        need = max(0, floors[payer_id][day] - balance - moved)
        if not need:
            continue
        choices = [
            (safe[source][departure] - used[source], source, departure, arrival, route)
            for source, (departure, arrival, route) in latest.items()
            if safe[source][departure] > used[source]
        ]
        for headroom, source, departure, arrival, route in sorted(choices, key=lambda choice: (-choice[0], choice[1])):
            amount = min(headroom, need)
            sent[(source, departure, arrival, route)] += amount
            used[source] += amount
            moved += amount
            need -= amount
            if not need:
                break
        if need:
            return {"feasible": False, "moved": 0, "transfers": [], "balances": original}

    credits = Counter()
    transfers = []
    for (source, departure, arrival, route), amount in sorted(
        sent.items(), key=lambda item: (item[0][1], item[0][2], item[0][0]),
    ):
        transfers.append({"route_id": route, "source_id": source, "destination_id": payer_id,
                          "departure": departure, "arrival": arrival, "amount": amount})
        credits[arrival] += amount
    total = 0
    funded = []
    for day, amount in enumerate(original):
        total += credits[day]
        funded.append(amount + total)
    return {"feasible": True, "moved": moved, "transfers": transfers, "balances": funded}
