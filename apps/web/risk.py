"""Daily cash risk, using common seeded draws for comparisons."""
from collections import Counter, defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from random import Random

from .forecast import historical_cash, integer, liquid_ids, month_end, parse_date, projected_events
from .reports import DAY, Window, month_start, movements
from .values import ApiError, money


DRIVERS = {
    "income": "Scheduled income uncertainty",
    "expenses": "Scheduled bill variation",
    "reimbursements": "Reimbursement delays",
    "shocks": "Unexpected expenses",
    "history": "Unscheduled historical activity",
}


@dataclass
class Model:
    today: date
    dates: list
    opening: int
    scope: set
    events: list
    base: list
    paths: list
    driver_hits: dict
    reserve: int
    price: int
    purchase: int
    confidence: int
    history_months: int
    seed: int


def parameters(data, today):
    days = integer(data.get("days", 180), 30, 730, "days")
    purchase_on = parse_date(data.get("purchase_on") or (today + 30 * DAY).isoformat(), "purchase_on")
    purchase = (purchase_on - today).days
    price = money(data.get("purchase_amount", "0"), "purchase_amount")
    reserve = money(data.get("reserve", "0"), "reserve")
    shock_amount = money(data.get("shock_amount", "300"), "shock_amount")
    if not 0 <= purchase <= days or min(price, reserve, shock_amount) < 0:
        raise ApiError("Use nonnegative amounts and a purchase date within the forecast horizon.", "purchase_on")
    result = {
        "days": days, "purchase": purchase, "price": price, "reserve": reserve, "shock_amount": shock_amount,
        "paths": integer(data.get("paths", 350), 100, 1000, "paths"),
        "seed": integer(data.get("seed", 42), 0, 2**32 - 1, "seed"),
        "confidence": integer(data.get("confidence", 90), 1, 100, "confidence"),
        "delay_days": integer(data.get("delay_days", 21), 1, 90, "delay_days"),
        "history": data.get("history") is True,
    }
    for key, default in (("income_pct", 5), ("income_miss_pct", 0), ("expense_pct", 8),
                         ("delay_pct", 25), ("shock_pct", 20)):
        result[key] = integer(data.get(key, default), 0, 100, key)
    return result


def residual_months(ledger, scope, conn, today):
    opening = min(account["opened_on"] for account in ledger.accounts if account["id"] in scope)
    windows = [month_start(today, -offset) for offset in range(12, 0, -1)]
    windows = [month for month in windows if month.isoformat() >= opening]
    known = {row[0] for row in conn.execute("SELECT entry_id FROM commitment_occurrences WHERE entry_id IS NOT NULL")}
    known_names = {(row["kind"], row["account_id"], row["name"].strip().casefold())
                   for row in conn.execute("SELECT kind, account_id, name FROM commitments WHERE archived_on IS NULL")}
    grouped = {month.isoformat()[:7]: Counter() for month in windows}
    for entry in ledger.entries:
        if entry["kind"] not in ("income", "expense") or entry.get("shared") or entry["id"] in known:
            continue
        if (entry["kind"], entry["account_id"], entry["name"].strip().casefold()) in known_names:
            continue
        month = grouped.get(entry["posted_on"][:7])
        if month is not None:
            month[int(entry["posted_on"][-2:])] += sum(
                delta for account, delta in movements(entry) if account in scope)
    return list(grouped.values())


def path_from(opening, events):
    cash = opening
    levels = []
    for event in events:
        cash += event
        levels.append(cash)
    return levels


def scaled_change(amount, percent):
    product = amount * percent
    scaled = (abs(product) + 50) // 100
    return -scaled if product < 0 else scaled


def draw_effects(rng, settings, events, size, month_ranges, history):
    effects = {key: [0] * size for key in DRIVERS}
    for index, event in events:
        amount = event["amount"]
        if event["kind"] == "income" and amount > 0 and event.get("purpose") != "return":
            pct = settings["income_pct"]
            delta = scaled_change(amount, rng.randint(-pct, pct))
            if rng.randrange(100) < settings["income_miss_pct"]:
                delta = -amount
            effects["income"][index] += delta
        elif event["kind"] == "expense" and amount < 0:
            pct = settings["expense_pct"]
            effects["expenses"][index] += scaled_change(amount, rng.randint(-pct, pct))
        elif event.get("scheduled_settlement_id") and amount > 0:
            delay = rng.randint(1, settings["delay_days"])
            if rng.randrange(100) < settings["delay_pct"]:
                effects["reimbursements"][index] -= amount
                if index + delay < size:
                    effects["reimbursements"][index + delay] += amount
    for start, end, first, last, calendar_days in month_ranges:
        shock_day = rng.randrange(start, end)
        if rng.randrange(100 * calendar_days) < settings["shock_pct"] * (end - start):
            effects["shocks"][shock_day] -= settings["shock_amount"]
        if history:
            for day, amount in rng.choice(history).items():
                day = min(day, last)
                if day >= first:
                    effects["history"][start + day - first] += amount
    return effects


def build_model(database, data, today, *, conn=None):
    settings = parameters(data, today)
    dates = [today + index * DAY for index in range(settings["days"] + 1)]
    with (nullcontext(conn) if conn is not None else database.connection(snapshot=True)) as conn:
        ledger = database.ledger(conn=conn)
        scope = liquid_ids(ledger)
        if not scope:
            raise ApiError("Add a checking, cash or savings account to run a cash forecast.", "account")
        opening = historical_cash(ledger, [Window(today, today)], scope)[0]
        events = [(int((row["date"] - today).days), row)
                  for row in projected_events(database, today, dates[-1], conn=conn) if row["account_id"] in scope]
        history = residual_months(ledger, scope, conn, today) if settings["history"] else []
    if settings["history"] and len(history) < 3:
        raise ApiError("Historical sampling requires at least three completed months since opening your first cash account.", "history")

    base_events = [0] * len(dates)
    for index, event in events:
        base_events[index] += event["amount"]
    base = path_from(opening, base_events)
    grouped = defaultdict(list)
    for index, day in enumerate(dates):
        grouped[month_start(day)].append(index)
    month_ranges = [(group[0], group[-1] + 1, dates[group[0]].day, dates[group[-1]].day, month_end(dates[group[0]]).day)
                    for group in grouped.values()]
    reserve, purchase, price = settings["reserve"], settings["purchase"], settings["price"]
    cost = [0 if index < purchase else price for index in range(len(dates))]
    rng = Random(settings["seed"])
    paths, without = [], Counter()
    for _ in range(settings["paths"]):
        effects = {key: path_from(0, series) for key, series in
                   draw_effects(rng, settings, events, len(dates), month_ranges, history).items()}
        cash = [sum(values) for values in zip(base, *effects.values())]
        paths.append(cash)
        paid = [balance - expense for balance, expense in zip(cash, cost)]
        for key, series in effects.items():
            without[key] += all(balance - delta >= reserve for balance, delta in zip(paid, series))
    return Model(today, dates, opening, scope, events, base, paths, without, reserve, price, purchase,
                 settings["confidence"], len(history), settings["seed"])


def path_minimum(path, index, price):
    after = min(path[index:]) - price
    return min(min(path[:index], default=after), after)


def quantile(values, position):
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) * position // 100]


def capacity_curve(model):
    """Price is feasible only if the same draw stays above reserve before and after purchase."""
    size = len(model.dates)
    amounts = [[] for _ in model.dates]
    for path in model.paths:
        after = [0] * size
        minimum = path[-1]
        for index in range(size - 1, -1, -1):
            minimum = min(minimum, path[index])
            after[index] = minimum
        prior_safe = True
        for index, value in enumerate(path):
            if prior_safe:
                amounts[index].append(after[index] - model.reserve)
            prior_safe = prior_safe and value >= model.reserve
    required = (len(model.paths) * model.confidence + 99) // 100
    result = []
    for values in amounts:
        cutoff = sorted(values, reverse=True)[required - 1] if len(values) >= required else -1
        result.append(cutoff if cutoff >= 0 else None)
    return result


def evaluate(model, index, price, adjustments=None):
    if adjustments is None:
        minima = [path_minimum(path, index, price) for path in model.paths]
    else:
        deltas = [value - (price if day >= index else 0) for day, value in enumerate(adjustments)]
        minima = [min(balance + delta for balance, delta in zip(path, deltas)) for path in model.paths]
    hits = sum(value >= model.reserve for value in minima)
    return {"safe_paths": hits, "success": 100 * hits / len(model.paths),
            "low_cash": str(quantile(minima, 10)), "median_low_cash": str(quantile(minima, 50))}


def deterministic_stress(model):
    paid = [balance - (model.price if index >= model.purchase else 0) for index, balance in enumerate(model.base)]
    breach = next((model.dates[index].isoformat() for index, value in enumerate(paid)
                   if value < model.reserve), None)
    thresholds = {"baseline_breach": breach, "extra_expense": None, "income_cut_pct": None, "reimbursement": None}
    if breach:
        return thresholds
    thresholds["extra_expense"] = str(min(paid[model.purchase:]) - model.reserve + 1)
    incomes = Counter()
    for index, event in model.events:
        if event["kind"] == "income" and event.get("purpose") != "return":
            incomes[index] += event["amount"]
    for pct in range(1, 101):
        loss = 0
        for index, balance in enumerate(paid):
            loss += scaled_change(incomes[index], pct)
            if balance - loss < model.reserve:
                thresholds["income_cut_pct"] = pct
                break
        if thresholds["income_cut_pct"] is not None:
            break
    reimbursement = next(((index, event) for index, event in model.events
                          if event.get("scheduled_settlement_id") and event["amount"] > 0), None)
    if reimbursement:
        index, event = reimbursement
        limit = next((test - index + 1 for test, balance in enumerate(paid[index:], index)
                      if balance - event["amount"] < model.reserve), None)
        thresholds["reimbursement"] = {"name": event["name"], "due_on": model.dates[index].isoformat(),
                                       "first_failing_delay": limit}
    return thresholds


def analyze(model):
    outcome = evaluate(model, model.purchase, model.price)
    breaches = Counter()
    for path in model.paths:
        for index, balance in enumerate(path):
            if balance - (model.price if index >= model.purchase else 0) < model.reserve:
                breaches[model.dates[index].isoformat()[:7]] += 1
                break
    indices = sorted({0, model.purchase, len(model.dates) - 1, *range(7, len(model.dates), 7)})
    points = []
    for index in indices:
        price = model.price if index >= model.purchase else 0
        values = sorted(path[index] - price for path in model.paths)
        points.append({"date": model.dates[index].isoformat(), "baseline": str(model.base[index] - price),
                       **{f"p{p}": str(values[(len(values) - 1) * p // 100]) for p in (10, 50, 90)}})
    capacity = capacity_curve(model)
    chosen = capacity[model.purchase]
    earliest = next((day.isoformat() for day, available in zip(model.dates, capacity)
                     if available is not None and available >= model.price), None)
    capacity_points = [{"date": model.dates[index].isoformat(),
                        "capacity": str(capacity[index]) if capacity[index] is not None else None} for index in indices]
    drivers = [
        {"key": key, "name": name, "improvement": 100 * (model.driver_hits[key] - outcome["safe_paths"]) / len(model.paths)}
        for key, name in DRIVERS.items()
    ]
    return dict(outcome, points=points, reserve=str(model.reserve), opening=str(model.opening),
                paths=len(model.paths), seed=model.seed, account_scope=sorted(model.scope),
                history_months=model.history_months, confidence=model.confidence,
                purchase_on=model.dates[model.purchase].isoformat(), purchase_amount=str(model.price),
                first_breaches=dict(sorted(breaches.items())),
                capacity=str(chosen) if chosen is not None else None, earliest_purchase=earliest,
                capacity_points=capacity_points, stress=deterministic_stress(model),
                drivers=sorted(drivers, key=lambda item: -item["improvement"]))


def investment_adjustments(model, monthly):
    by_day = [0] * len(model.dates)
    reductions = Counter()
    if monthly < 0:
        for index, event in model.events:
            if event["kind"] == "investment" and event["amount"] < 0:
                month = month_start(model.dates[index])
                amount = min(-event["amount"], -monthly - reductions[month])
                by_day[index] += amount
                reductions[month] += amount
    elif monthly > 0:
        for index, day in enumerate(model.dates):
            if day == month_end(day):
                by_day[index] -= monthly
    return path_from(0, by_day)
