"""Check purchasing-account cash against household funding constraints."""

from collections import defaultdict
from datetime import date

from .forecast import LIQUID, integer, parse_date, projected_events
from .funding_policy import funding_policy, handle_funding_policy
from .funding_solver import earliest_without_transfer, solve_funding
from .goals import load_goals
from .reports import DAY, movements
from .values import ApiError, identifier, line, money


MAX_DAYS = 366


def _account_data(handler, database, ledger, conn):
    policy = funding_policy(handler, database, ledger, conn=conn)
    earmarks = defaultdict(int)
    for goal in load_goals(conn):
        for link in goal["funding"]:
            earmarks[link["account_id"]] += link["earmarked"]
    for account in policy["accounts"]:
        account["protected"] = str(earmarks[account["id"]])
    return policy


def _opening_balances(ledger, ids, today):
    balances = {row["id"]: row["opening_balance"] for row in ledger.accounts
                if row["id"] in ids and row["opened_on"] <= today.isoformat()}
    for entry in ledger.entries:
        if entry["posted_on"] > today.isoformat():
            break
        for account_id, delta in movements(entry):
            if account_id in balances:
                balances[account_id] += delta
    return balances


def _first_shortfall(values, floor, dates, activity):
    for index, (value, minimum) in enumerate(zip(values, floor)):
        if value < minimum:
            day = dates[index]
            recent = day - 7 * DAY
            relevant = [event for when, events in sorted(activity.items()) for event in events
                        if when <= day and (when >= recent or event["kind"] == "purchase")]
            return {
                "date": day.isoformat(), "balance": str(value),
                "floor": str(minimum), "amount": str(minimum - value),
                "events": [{"date": event["date"].isoformat(), "name": event["name"],
                            "amount": str(event["amount"]), "kind": event["kind"]}
                           for event in relevant],
            }
    return None


def _arrival(start, delay):
    date = start
    while delay:
        date += DAY
        if date.weekday() < 5:
            delay -= 1
    return date


def _eligible_routes(policy, accounts, dates, payer_id):
    indices = {date: index for index, date in enumerate(dates)}
    routes = []
    for route in policy["routes"]:
        if route["destination_id"] != payer_id or not route.get("active", True):
            continue
        source = accounts[route["source_id"]]
        available = max(source["opened_on"], source["available_on"] or source["opened_on"])
        routes.append(dict(route, arrivals={
            index: indices[arrival] for index, day in enumerate(dates)
            if day.weekday() < 5 and day.isoformat() >= available
            if (arrival := _arrival(day, route["days"])) in indices
        }))
    return routes


def _daily_series(opening, events, dates):
    changes = defaultdict(int)
    for event in events:
        changes[event["account_id"], event["date"]] += event["amount"]
    balances = {}
    for account, balance in opening.items():
        series = []
        for date in dates:
            balance += changes[account, date]
            series.append(balance)
        balances[account] = series
    return balances


def _purchase_balance(balances, amount, start):
    return [value - (amount if index >= start else 0) for index, value in enumerate(balances)]


def _earliest_with_transfers(balances, floors, routes, payer_id, amount, first):
    """Feasibility can only increase as the same purchase moves later."""
    original = balances[payer_id]
    left, right = first, len(original)
    while left < right:
        index = (left + right) // 2
        balances[payer_id] = _purchase_balance(original, amount, index)
        if solve_funding(balances, floors, routes, payer_id)["feasible"]:
            right = index
        else:
            left = index + 1
    balances[payer_id] = original
    return left if left < len(original) else None


def _scenario(opening, events, dates, floors, routes, payer, purchase):
    balances = _daily_series(opening, events, dates)
    payer_id = payer["id"]
    floor = floors[payer_id]
    payer_events = defaultdict(list)
    for event in events:
        if event["account_id"] == payer_id:
            payer_events[event["date"]].append(event)
    baseline = _first_shortfall(balances[payer_id], floor, dates, payer_events)
    amount, requested_date, name = purchase
    start = (requested_date - dates[0]).days
    available = date.fromisoformat(payer["available_on"]) if payer["available_on"] else dates[0]
    blocked = bool(amount and requested_date < available)
    first = max(start, (available - dates[0]).days if amount else start)
    original = balances[payer_id]
    earliest = earliest_without_transfer(original, floor, amount, first)
    if amount:
        payer_events[requested_date].append(
            {"date": requested_date, "name": name, "amount": -amount, "kind": "purchase"}
        )
        balances[payer_id] = _purchase_balance(original, amount, start)
    issue = _first_shortfall(balances[payer_id], floor, dates, payer_events)
    solution = solve_funding(balances, floors, routes, payer_id) if not blocked else {
        "feasible": False, "moved": 0, "transfers": [], "balances": balances[payer_id],
    }
    selected = balances[payer_id]
    earliest_funded = start if solution["feasible"] else earliest
    if amount and not solution["feasible"] and routes:
        balances[payer_id] = original
        earliest_funded = _earliest_with_transfers(balances, floors, routes, payer_id, amount, max(first, start + 1))
    return {
        "safe": issue is None and not blocked, "can_fund": solution["feasible"],
        "baseline_first_shortfall": baseline, "first_shortfall": issue,
        "earliest_on": dates[earliest].isoformat() if earliest is not None else None,
        "earliest_funded_on": dates[earliest_funded].isoformat() if earliest_funded is not None else None,
        "moved": str(solution["moved"]),
        "transfers": [{
            "source_id": item["source_id"], "destination_id": item["destination_id"],
            "route_id": item["route_id"], "sent_on": dates[item["departure"]].isoformat(),
            "arrives_on": dates[item["arrival"]].isoformat(), "amount": str(item["amount"]),
        } for item in solution["transfers"]],
        "points": [{"date": day.isoformat(), "balance": str(value), "floor": str(minimum),
                    "funded_balance": str(funded) if solution["feasible"] else None}
                   for day, value, minimum, funded in zip(dates, selected, floor, solution["balances"])],
    }


def _reimbursement_stress(events, delay):
    shifted, changes = [], []
    for event in events:
        if delay and event.get("scheduled_settlement_id") and event["amount"] > 0:
            late = event["date"] + delay * DAY
            changes.append({"name": event["name"], "scheduled_on": event["date"].isoformat(),
                            "arrives_on": late.isoformat(), "amount": str(event["amount"]),
                            "account_id": event["account_id"]})
            event = dict(event, date=late)
        shifted.append(event)
    return shifted, changes


def funding_check(handler, database, data, today):
    payer_id = identifier(data.get("account_id"), "account_id")
    amount = money(data.get("amount", "0"), "amount")
    if amount < 0:
        raise ApiError("Enter a nonnegative purchase amount.", "amount")
    purchase_date = parse_date(data.get("purchase_on", today.isoformat()), "purchase_on")
    days = integer(data.get("days", 60), 1, MAX_DAYS, "days")
    delay = integer(data.get("reimbursement_delay", 7), 0, 90, "reimbursement_delay")
    label = line(data.get("label", ""), "label", 100, required=False) or ("Proposed purchase" if amount else "Scheduled payments")
    dates = [today + offset * DAY for offset in range(days)]
    if not today <= purchase_date <= dates[-1]:
        raise ApiError("Choose a date within the forecast period or extend the horizon.", "purchase_on")
    with database.connection(snapshot=True) as conn:
        ledger = database.ledger(conn)
        policy = _account_data(handler, database, ledger, conn)
        accounts = {row["id"]: row for row in policy["accounts"]}
        payer = accounts.get(payer_id)
        if not payer or payer["kind"] not in LIQUID:
            raise ApiError("Choose a checking, cash or savings account to pay from.", "account_id")
        opening = _opening_balances(ledger, accounts, today)
        events = [event for event in projected_events(database, today, dates[-1], conn)
                  if event["account_id"] in opening]
    if payer_id not in opening:
        raise ApiError("Choose an account that is already open.", "account_id")
    policy["routes"] = [route for route in policy["routes"] if route["source_id"] in opening]
    floors = {key: [int(accounts[key]["reserve"]) + int(accounts[key]["protected"])] * days for key in opening}
    routes = _eligible_routes(policy, accounts, dates, payer_id)
    purchase = amount, purchase_date, label
    expected = _scenario(opening, events, dates, floors, routes, payer, purchase)
    shifted, changes = _reimbursement_stress(events, delay)
    late = _scenario(opening, shifted, dates, floors, routes, payer, purchase) if changes else expected
    blocked_until = payer["available_on"] if amount and payer["available_on"] and purchase_date.isoformat() < payer["available_on"] else None
    return {"today": today.isoformat(), "through": dates[-1].isoformat(), "account_id": payer_id,
            "amount": str(amount), "purchase_on": purchase_date.isoformat(), "label": label,
            "blocked_until": blocked_until, "reimbursement_delay": delay, "delayed_reimbursements": changes,
            "base": expected, "late": late}


def handle_funding(handler, path, query, method, today):
    if not path.startswith("/api/funding/"):
        return None
    database = handler.database
    if path == "/api/funding/setup" and method == "GET":
        with database.connection(snapshot=True) as conn:
            policy = _account_data(handler, database, database.ledger(conn), conn)
            if handler.workspace.get("demo_role") == "family":
                payer = next((account for account in policy["accounts"] if account["name"] == "Family checking"), None)
                seeded = conn.execute("SELECT seeded_on FROM sample_seed WHERE role='family'").fetchone()
                if payer and seeded:
                    purchase_on = date.fromisoformat(seeded["seeded_on"]) + 10 * DAY
                    if purchase_on >= today:
                        policy["example"] = {
                            "account_id": payer["id"], "amount": "900", "purchase_on": purchase_on.isoformat(),
                            "days": 60, "reimbursement_delay": 21, "label": "Sample Family purchase",
                        }
        return {"today": today.isoformat(), **policy}, 200
    if path == "/api/funding/check" and method == "POST":
        return funding_check(handler, database, handler.read_data(8192), today), 200
    result = handle_funding_policy(handler, path, method, today)
    if result is None:
        raise ApiError("This funding action was not found.", status=404)
    return result
