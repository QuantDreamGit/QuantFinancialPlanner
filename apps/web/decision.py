"""Saved risk forecasts, decision searches, and competing goal comparisons."""
import json
import re
from contextlib import nullcontext
from datetime import date, datetime, timezone

from .forecast import historical_cash, integer, month_end, projected_events
from .goals import PROJECTION_MONTHS, load_goals, project_goals
from .reports import DAY, Window, month_start, timeline
from .risk import analyze, build_model, evaluate, investment_adjustments
from .storage import row_by_id
from .values import ApiError, identifier, line, money


SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    issued_on TEXT NOT NULL,
    assumptions TEXT NOT NULL,
    result TEXT NOT NULL
);
"""
BASE = "/api/decision/"


def initialize_decision(database):
    with database.connection() as conn:
        conn.executescript(SCHEMA)


def persisted_run(database, item_id):
    with database.connection() as conn:
        row = row_by_id(conn, "decision_runs", item_id)
        return dict(row, assumptions=json.loads(row["assumptions"]), result=json.loads(row["result"]))


def save_run(database, data, today):
    name = line(data.get("name"), "name", 80)
    assumptions = data.get("assumptions")
    if not isinstance(assumptions, dict):
        raise ApiError("Enter the forecast assumptions first.", "assumptions")
    result = analyze(build_model(database, assumptions, today))
    with database.connection(write=True) as conn:
        item_id = conn.execute(
            "INSERT INTO decision_runs (name, issued_on, assumptions, result) VALUES (?, ?, ?, ?)",
            (name, today.isoformat(), json.dumps(assumptions), json.dumps(result)),
        ).lastrowid
    return {"id": item_id, "name": name, "issued_on": today.isoformat(), "assumptions": assumptions, "result": result}


def rounded_fraction(numerator, denominator):
    absolute = (abs(numerator) + denominator // 2) // denominator
    return -absolute if numerator < 0 else absolute


def score_runs(conn):
    for row in conn.execute("SELECT id, name, issued_on, result FROM decision_runs"):
        result = json.loads(row["result"])
        yield {"id": row["id"], "name": row["name"], "issued_on": row["issued_on"], "source": "risk",
               "scope": result["account_scope"], "points": result["points"]}
    for row in conn.execute("SELECT id, name, saved_on, result FROM forecast_comparisons"):
        result = json.loads(row["result"])
        yield {"id": row["id"], "name": row["name"], "issued_on": row["saved_on"], "source": "what-if",
               "scope": result.get("account_scope", []),
               "points": [{"date": point["day"], "p50": point["baseline"]} for point in result["points"]
                          if not point.get("opening")]}
    for row in conn.execute("SELECT id, name, updated_at, input, result FROM scenarios"):
        result = json.loads(row["result"])
        issued_on = datetime.fromisoformat(row["updated_at"]).replace(tzinfo=timezone.utc).astimezone().date().isoformat()
        projection = result["plans"][0]["projection"] if result["plans"] else []
        yield {"id": row["id"], "name": row["name"], "issued_on": issued_on, "source": "purchase",
               "scope": json.loads(row["input"]).get("accountScope", []),
               "points": [{"date": month_end(date.fromisoformat(point["month"] + "-01")).isoformat(),
                           "p50": str(int(point["availableCash"]) + int(point["earmarkedCash"]))} for point in projection]}


def scorecard(database, today):
    with database.connection(snapshot=True) as conn:
        ledger = database.ledger(conn=conn)
        known_accounts = {account["id"] for account in ledger.accounts}
        runs = sorted(score_runs(conn), key=lambda run: (run["issued_on"], run["id"]), reverse=True)
    dated_runs = []
    points_by_scope = {}
    for run in runs:
        scope = frozenset(run.pop("scope"))
        points = run.pop("points")
        if not scope or not scope.issubset(known_accounts):
            dated_runs.append((run, [], scope, "account_scope_unavailable"))
            continue
        eligible = [point for point in points if run["issued_on"] < point["date"] < today.isoformat()]
        points_by_scope.setdefault(scope, set()).update(point["date"] for point in eligible)
        status = "observed" if eligible else "awaiting_observations" if points else "no_projection"
        dated_runs.append((run, eligible, scope, status))
    actual_by_scope = {}
    for scope, observed in points_by_scope.items():
        ordered = sorted(observed)
        span = [Window(day, day) for day in map(date.fromisoformat, ordered)]
        actual_by_scope[scope] = dict(zip(ordered, historical_cash(ledger, span, scope)))
    rows = []
    all_errors = []
    all_inside = all_banded = 0
    for run, points, scope, status in dated_runs:
        actuals = actual_by_scope.get(scope, {})
        errors = [actuals[point["date"]] - int(point["p50"]) for point in points]
        banded = [point for point in points if "p10" in point]
        inside = sum(int(point["p10"]) <= actuals[point["date"]] <= int(point["p90"]) for point in banded)
        all_errors.extend(errors)
        all_inside += inside
        all_banded += len(banded)
        rows.append(dict(run, status=status, **score_metrics(errors, inside, len(banded))))
    return {"runs": rows, "overall": score_metrics(all_errors, all_inside, all_banded)}


def score_metrics(errors, inside, banded):
    count = len(errors)
    return {
        "count": count, "inside_band": inside, "banded": banded,
        "coverage": 100 * inside / banded if banded else None,
        "bias": str(rounded_fraction(sum(errors), count)) if count else None,
        "mae": str(rounded_fraction(sum(map(abs, errors)), count)) if count else None,
    }


def goal_context(database, today, conn=None):
    with (nullcontext(conn) if conn is not None else database.connection(snapshot=True)) as conn:
        ledger = database.ledger(conn=conn)
        goals = load_goals(conn)
        events = projected_events(database, today, month_start(today, PROJECTION_MONTHS + 1) - DAY, conn=conn) if goals else []
    _, balances = timeline(ledger, [Window(today, today)])
    return goals, balances, events


def paired_goals(goals, data):
    source = identifier(data.get("from_id"), "from_id")
    target = identifier(data.get("to_id"), "to_id")
    account = identifier(data.get("account_id"), "account_id")
    by_id = {item["id"]: item for item in goals}
    if source == target or source not in by_id or target not in by_id:
        raise ApiError("Choose two different goals.", "to_id")
    funding = [{row["account_id"]: row for row in by_id[key]["funding"]} for key in (source, target)]
    if account not in funding[0] or account not in funding[1]:
        raise ApiError("Choose a savings account linked to both goals.", "account_id")
    return source, target, account, funding[0][account]["monthly"], by_id


def redirect_goals(goals, pair, amount):
    source, target, account, _, _ = pair
    return [dict(goal, funding=[
        dict(link, monthly=link["monthly"] + (amount if goal["id"] == target else -amount))
        if link["account_id"] == account and goal["id"] in (source, target) else link
        for link in goal["funding"]
    ]) for goal in goals]


def goal_point(today, goals, balances, events, pair, amount):
    estimates = project_goals(redirect_goals(goals, pair, amount), balances, today, events)[2]
    source, target = pair[:2]
    return {
        "redirect": str(amount), "from_on": estimates[source].isoformat() if estimates[source] else None,
        "to_on": estimates[target].isoformat() if estimates[target] else None,
    }


def goal_tradeoff(database, data, today):
    goals, balances, events = goal_context(database, today)
    pair = paired_goals(goals, data)
    amounts = sorted({pair[3] * index // 10 for index in range(11)})
    return {"source": pair[4][pair[0]]["name"], "target": pair[4][pair[1]]["name"],
            "today": today.isoformat(), "horizon_months": PROJECTION_MONTHS,
            "points": [goal_point(today, goals, balances, events, pair, amount) for amount in amounts]}


def goal_balanced_events(model, events, funding_account, index, adjustments, monthly):
    """Project the purchase and contribution changes in their funding accounts."""
    changes = [dict(date=model.dates[index], amount=-model.price, account_id=funding_account)]
    reductions = {}
    for day_index, event in model.events:
        if monthly >= 0 or event["kind"] != "investment" or event["amount"] >= 0:
            continue
        month = month_start(model.dates[day_index])
        value = min(-event["amount"], -monthly - reductions.get(month, 0))
        reductions[month] = reductions.get(month, 0) + value
        changes.append(dict(date=model.dates[day_index], amount=value, account_id=event["account_id"]))
    if monthly > 0:
        previous = 0
        for day, value in zip(model.dates, adjustments):
            if value != previous:
                changes.append(dict(date=day, account_id=funding_account, amount=value - previous))
            previous = value
    return events + changes


def optimizer(database, data, today):
    assumptions = data.get("assumptions")
    if not isinstance(assumptions, dict):
        raise ApiError("Enter the forecast assumptions first.", "assumptions")
    delay = integer(data.get("delay_days", 60), 0, 180, "delay_days")
    flex = money(data.get("investment_flex", "100"), "investment_flex")
    if flex < 0:
        raise ApiError("Investment flexibility must be zero or greater.", "investment_flex")
    pair = None
    with database.connection(snapshot=True) as conn:
        model = build_model(database, assumptions, today, conn=conn)
        if data.get("from_id") or data.get("to_id"):
            goals, balances, events = goal_context(database, today, conn)
            pair = paired_goals(goals, data)
    final = min(model.purchase + delay, len(model.dates) - 1)
    placement = sorted({model.purchase + (final - model.purchase) * point // 4 for point in range(5)})
    monthly_steps = sorted({-flex, 0, flex})
    adjustments = {value: investment_adjustments(model, value) for value in monthly_steps}
    if pair:
        funding_account = identifier(data.get("funding_account"), "funding_account")
        if funding_account not in model.scope:
            raise ApiError("Choose the cash account that would fund the purchase and additional investing.", "funding_account")
        redirects = sorted({0, pair[3] // 2, pair[3]})
    else:
        redirects = [0]

    candidates = []
    for index in placement:
        for monthly in monthly_steps:
            outcome = evaluate(model, index, model.price, adjustments[monthly])
            for redirect in redirects:
                row = dict(outcome, purchase_on=model.dates[index].isoformat(),
                           investment_change=str(monthly), redirect=str(redirect))
                metrics = [row["safe_paths"], -index, monthly]
                if pair:
                    modeled = goal_balanced_events(model, events, funding_account, index, adjustments[monthly], monthly)
                    point = goal_point(today, goals, balances, modeled, pair, redirect)
                    row.update(point)
                    metrics.extend(-date.fromisoformat(point[key]).toordinal() if point[key] else -date.max.toordinal()
                                   for key in ("from_on", "to_on"))
                row["metrics"] = metrics
                candidates.append(row)

    qualified = [row for row in candidates if row["safe_paths"] * 100 >= len(model.paths) * model.confidence]
    considered = qualified or candidates

    def dominates(a, b):
        return all(left >= right for left, right in zip(a, b)) and any(left > right for left, right in zip(a, b))

    frontier = [row for row in considered if not any(dominates(other["metrics"], row["metrics"]) for other in considered)]
    ranked = sorted(frontier, key=lambda row: (-row["safe_paths"], row["purchase_on"], -int(row["investment_change"])))
    return {
        "candidates": len(candidates), "qualified": len(qualified), "confidence": model.confidence,
        "paths": len(model.paths), "frontier": [{key: value for key, value in row.items() if key != "metrics"} for row in ranked],
        "goal_names": [pair[4][key]["name"] for key in pair[:2]] if pair else [],
    }


def handle_decision(handler, path, query, method, today):
    if not path.startswith(BASE):
        return None
    database = getattr(handler, "database", handler.server.database)
    resource = path[len(BASE):]
    if method == "GET":
        if resource == "setup":
            with database.connection(snapshot=True) as conn:
                ledger = database.ledger(conn=conn)
                goals = load_goals(conn)
            return {"today": today.isoformat(), "accounts": [
                {key: account[key] for key in ("id", "name", "kind")} for account in ledger.accounts
                if account["kind"] in ("cash", "checking", "savings")
            ], "goals": [{"id": goal["id"], "name": goal["name"],
                          "accounts": [row["account_id"] for row in goal["funding"]]} for goal in goals]}, 200
        if resource == "scorecard":
            return scorecard(database, today), 200
        if resource == "runs":
            with database.connection() as conn:
                return [dict(row) for row in conn.execute(
                    "SELECT id, name, issued_on FROM decision_runs ORDER BY id DESC"
                )], 200
        match = re.fullmatch(r"runs/([1-9][0-9]{0,17})", resource)
        if match:
            return persisted_run(database, int(match[1])), 200
    if method == "POST":
        if resource == "analyze":
            return analyze(build_model(database, handler.read_data(16_384), today)), 200
        if resource == "runs":
            return save_run(database, handler.read_data(16_384), today), 201
        if resource == "tradeoff":
            return goal_tradeoff(database, handler.read_data(16_384), today), 200
        if resource == "optimize":
            return optimizer(database, handler.read_data(16_384), today), 200
    raise ApiError("This decision analysis was not found.", status=404)
