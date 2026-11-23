"""Scheduled activity, liquidity projections, and historical plan comparisons."""
import calendar
import json
import re
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import date, timedelta

from .planning import calendar_month
from .reports import DAY, Window, month_start, movements, timeline
from .storage import row_by_id
from .values import ApiError, choice, identifier, line, money
from .workspaces import owned_account, owner_id


LIQUID = {"checking", "savings", "cash"}
KINDS = {"income", "expense", "investment", "transfer"}
CADENCES = {"once", "weekly", "monthly", "yearly"}
PURPOSES = {"payday", "bill", "subscription", "commitment"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    destination_id INTEGER REFERENCES accounts(id),
    category_id INTEGER REFERENCES categories(id),
    start_on TEXT NOT NULL,
    end_on TEXT,
    cadence TEXT NOT NULL,
    archived_on TEXT
);
CREATE TABLE IF NOT EXISTS commitment_occurrences (
    rule_id INTEGER NOT NULL REFERENCES commitments(id),
    due_on TEXT NOT NULL,
    entry_id INTEGER UNIQUE REFERENCES entries(id) ON DELETE CASCADE,
    PRIMARY KEY(rule_id, due_on)
);
CREATE TABLE IF NOT EXISTS forecast_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    saved_on TEXT NOT NULL,
    inputs TEXT NOT NULL,
    result TEXT NOT NULL
);
"""


def initialize_forecast(database):
    with database.connection() as conn:
        conn.executescript(SCHEMA)


def parse_date(value, field):
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value and 1900 <= day.year <= 9998:
            return day
    except (TypeError, ValueError):
        pass
    raise ApiError("Enter a calendar date between 1900 and 9998.", field)


def month_end(day):
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def add_months(day, offset):
    month = month_start(day, offset)
    return month.replace(day=min(day.day, month_end(month).day))


def occurrence_dates(anchor, cadence, start, end):
    """Generate occurrences without losing the original day after a short month."""
    start = max(start, anchor)
    if start > end:
        return
    if cadence == "once":
        if start == anchor:
            yield anchor
        return
    if cadence == "weekly":
        weeks = ((start - anchor).days + 6) // 7
        while (current := anchor + timedelta(weeks=weeks)) <= end:
            yield current
            weeks += 1
        return
    step = 12 if cadence == "yearly" else 1
    months = (start.year - anchor.year) * 12 + start.month - anchor.month
    offset = months // step * step
    while (current := add_months(anchor, offset)) <= end:
        if current >= start:
            yield current
        offset += step


def save_rule(database, data):
    name = line(data.get("name"), "name", 100)
    purpose = choice(data.get("purpose", "commitment"), PURPOSES, "purpose")
    kind = choice(data.get("kind"), KINDS, "kind")
    cadence = choice(data.get("cadence"), CADENCES, "cadence")
    start = parse_date(data.get("start_on"), "start_on")
    end = parse_date(data["end_on"], "end_on") if data.get("end_on") else None
    if end and end < start:
        raise ApiError("The last date must be on or after the first date.", "end_on")
    amount = money(data.get("amount"), "amount")
    if amount <= 0:
        raise ApiError("Enter an amount above €0.", "amount")
    account_id = owned_account(identifier(data.get("account_id"), "account_id"))
    destination_id = owned_account(identifier(data.get("destination_id"), "destination_id"), "destination_id")
    category_id = owned_account(identifier(data.get("category_id"), "category_id"), "category_id")
    with database.connection(write=True) as conn:
        account = row_by_id(conn, "accounts", account_id, "account_id")
        if account["kind"] not in LIQUID or start.isoformat() < account["opened_on"]:
            raise ApiError("Choose a cash or savings account already open on the first date.", "account_id")
        if kind in ("transfer", "investment"):
            destination = row_by_id(conn, "accounts", destination_id, "destination_id")
            if (destination_id == account_id or start.isoformat() < destination["opened_on"]
                    or (kind == "investment") != (destination["kind"] == "investment")):
                raise ApiError("Choose a different eligible destination account.", "destination_id")
        elif destination_id is not None:
            raise ApiError("This commitment uses a single account.", "destination_id")
        if category_id is not None:
            category = row_by_id(conn, "categories", category_id, "category_id")
            if category["kind"] != kind:
                raise ApiError("Choose a category for this transaction type.", "category_id")
        return conn.execute("""
            INSERT INTO commitments(name, purpose, kind, amount, account_id, destination_id,
                category_id, start_on, end_on, cadence) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, purpose, kind, amount, account_id, destination_id, category_id,
              start.isoformat(), end.isoformat() if end else None, cadence)).lastrowid


def archive_rule(database, rule_id, today):
    with database.connection(write=True) as conn:
        row_by_id(conn, "commitments", rule_id)
        conn.execute("UPDATE commitments SET archived_on = coalesce(archived_on, ?) WHERE id = ?",
                     (today.isoformat(), rule_id))


def rules_and_links(database, start=None, end=None, conn=None):
    with (nullcontext(conn) if conn is not None else database.connection(snapshot=True)) as conn:
        rules = [dict(row) for row in conn.execute("SELECT * FROM commitments ORDER BY name, id")]
        if getattr(database, "kind", None) == "group":
            profiles = {row["item_id"]: row["profile_name"] for row in conn.execute(
                "SELECT item_id, profile_name FROM group_catalog WHERE kind='accounts'")}
            for rule in rules:
                if owner_id(rule["id"]):
                    rule.update(profile_id=owner_id(rule["id"]), profile_name=profiles[rule["account_id"]])
        links = {}
        if start is not None:
            links = {(row["rule_id"], row["due_on"]): row["entry_id"] for row in conn.execute(
                "SELECT * FROM commitment_occurrences WHERE due_on BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            )}
        return rules, links


def rule_dates(rule, start, end):
    if rule["end_on"]:
        end = min(end, date.fromisoformat(rule["end_on"]))
    if rule["archived_on"]:
        end = min(end, date.fromisoformat(rule["archived_on"]) - DAY)
    return occurrence_dates(date.fromisoformat(rule["start_on"]), rule["cadence"], start, end)


def post_occurrence(database, data, today, *, conn=None):
    rule_id = owned_account(identifier(data.get("rule_id"), "rule_id"), "rule_id")
    due = parse_date(data.get("date"), "date")
    entry_id = identifier(data.get("entry_id"), "entry_id")
    skip = data.get("skip") is True
    restore = data.get("restore") is True
    if due > today and not (skip or restore):
        raise ApiError("Post commitments once their scheduled day arrives.", "date")
    if skip and entry_id:
        raise ApiError("A skipped occurrence cannot be linked to a transaction.", "entry_id")
    with (nullcontext(conn) if conn is not None else database.connection(write=True)) as conn:
        rule = row_by_id(conn, "commitments", rule_id)
        key = (rule_id, due.isoformat())
        existing = conn.execute("SELECT entry_id FROM commitment_occurrences WHERE rule_id = ? AND due_on = ?", key).fetchone()
        if restore:
            if existing and existing["entry_id"] is not None:
                raise ApiError("This occurrence is already posted.", status=409)
            if existing:
                conn.execute("DELETE FROM commitment_occurrences WHERE rule_id = ? AND due_on = ?", key)
            return None
        if existing:
            if (skip and existing["entry_id"] is None) or (not skip and existing["entry_id"] is not None
                                                           and entry_id in (None, existing["entry_id"])):
                return existing["entry_id"]
            raise ApiError("This scheduled occurrence has already been resolved.", status=409)
        if not list(rule_dates(rule, due, due)):
            raise ApiError("Choose a valid scheduled occurrence.", "date")
        if skip:
            entry_id = None
        elif entry_id is not None:
            entry = row_by_id(conn, "entries", entry_id, "entry_id")
            if any(entry[key] != rule[key] for key in ("kind", "account_id", "destination_id")):
                raise ApiError("Match a transaction with the same type and accounts.", "entry_id")
            if conn.execute("SELECT 1 FROM commitment_occurrences WHERE entry_id = ?", (entry_id,)).fetchone():
                raise ApiError("This transaction already matches a scheduled occurrence.", "entry_id", 409)
        else:
            for account_id in (rule["account_id"], rule["destination_id"]):
                if account_id and due.isoformat() < row_by_id(conn, "accounts", account_id)["opened_on"]:
                    raise ApiError("This occurrence is before an account’s opening date.", "date")
            entry_id = conn.execute("""
                INSERT INTO entries(kind, name, posted_on, amount, account_id, destination_id, category_id, note)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """, (rule["kind"], rule["name"], due.isoformat(), rule["amount"],
                  rule["account_id"], rule["destination_id"], rule["category_id"],
                  "Monthly investment return" if rule["purpose"] == "return" else "Posted from cash runway calendar")).lastrowid
        conn.execute("INSERT INTO commitment_occurrences VALUES(?, ?, ?)", (*key, entry_id))
        return entry_id


def calendar_occurrences(database, start, end, conn=None):
    rules, links = rules_and_links(database, start, end, conn)
    occurrences = []
    for rule in rules:
        dates = {day.isoformat() for day in rule_dates(rule, start, end)}
        dates.update(day for (rule_id, day) in links if rule_id == rule["id"])
        for day in dates:
            key = (rule["id"], day)
            entry_id = links.get(key)
            status = ("posted" if entry_id else "skipped") if key in links else "pending"
            occurrences.append(dict(rule, date=date.fromisoformat(day), status=status, entry_id=entry_id))
    return sorted(occurrences, key=lambda item: (item["date"], item["name"], item["id"]))


def shared_events(database, start, end, conn):
    from .shared import projected_cash
    return projected_cash(database, start, end, conn=conn)


def projected_events(database, start, end, conn=None):
    if end < start:
        return []
    with (nullcontext(conn) if conn is not None else database.connection(snapshot=True)) as conn:
        events = []
        for item in calendar_occurrences(database, start, end, conn):
            if item["status"] != "pending":
                continue
            amount = item["amount"]
            event = {key: item[key] for key in ("date", "name", "kind", "category_id", "account_id", "purpose",
                                               "profile_id", "profile_name") if key in item}
            events.append(dict(event, rule_id=item["id"], amount=amount if item["kind"] == "income" else -amount))
            if item["destination_id"] is not None:
                events.append(dict(event, rule_id=item["id"], account_id=item["destination_id"], amount=amount))
        return sorted(events + shared_events(database, start, end, conn), key=lambda item: item["date"])


def liquid_ids(ledger):
    return {account["id"] for account in ledger.accounts if account["kind"] in LIQUID}


def historical_cash(ledger, spans, scope):
    # Timeline's stocks use the accounts in the supplied ledger; filter transfers to
    # preserve their movement on scoped accounts while ignoring the other side.
    if scope == liquid_ids(ledger):
        balances = timeline(ledger, spans)[0] if spans else []
        return [int(point["cash"] or 0) + int(point["savings"] or 0) for point in balances]
    if not spans:
        return []
    ordered_openings = sorted((a for a in ledger.accounts if a["id"] in scope), key=lambda a: a["opened_on"])
    cash = account_index = entry_index = 0
    values = []
    for span in spans:
        end = span.end.isoformat()
        while account_index < len(ordered_openings) and ordered_openings[account_index]["opened_on"] <= end:
            cash += ordered_openings[account_index]["opening_balance"]
            account_index += 1
        while entry_index < len(ledger.entries) and ledger.entries[entry_index]["posted_on"] <= end:
            cash += sum(delta for account, delta in movements(ledger.entries[entry_index]) if account in scope)
            entry_index += 1
        values.append(cash)
    return values


def cash_path(database, today, end):
    with database.connection(snapshot=True) as conn:
        ledger = database.ledger(conn)
        pending = projected_events(database, today, end, conn)
    ids = liquid_ids(ledger)
    opening = historical_cash(ledger, [Window(today, today)], ids)[0]
    events = [event for event in pending if event["account_id"] in ids]
    return opening, events, ids


def listed_rule(rule):
    return dict(rule, amount=str(rule["amount"]))


def runway(database, month, today):
    start = month_start(month)
    end = month_end(start)
    past = [Window(start + index * DAY, start + index * DAY)
            for index in range(max(0, (min(end, today) - start).days + 1))]
    with database.connection(snapshot=True) as conn:
        ledger = database.ledger(conn)
        occurrences = calendar_occurrences(database, start, end, conn)
        future = projected_events(database, today, end, conn)
    ids = liquid_ids(ledger)
    actuals = historical_cash(ledger, past, ids)
    calendar_items = defaultdict(list)
    for item in occurrences:
        calendar_items[item["date"]].append(dict(listed_rule(item), date=item["date"].isoformat()))
    upcoming, changes = defaultdict(list), Counter()
    for event in future:
        if event.get("shared"):
            upcoming[event["date"]].append(dict(event, date=event["date"].isoformat(), amount=str(event["amount"])))
        if event["account_id"] in ids:
            changes[event["date"]] += event["amount"]
    projected = historical_cash(ledger, [Window(today, today)], ids)[0]
    for day in sorted(changes):
        if day < start:
            projected += changes[day]
    days = []
    for index in range(end.day):
        day = start + index * DAY
        if day >= today:
            projected += changes[day]
        days.append({
            "date": day.isoformat(),
            "actual": str(actuals[index]) if index < len(actuals) and ids else None,
            "projected": str(projected) if day >= today and ids else None,
            "items": calendar_items[day],
            "shared": upcoming[day],
        })
    return {"month": start.isoformat()[:7], "today": today.isoformat(), "days": days}


def integer(value, minimum, maximum, field):
    if type(value) is int and minimum <= value <= maximum:
        return value
    raise ApiError(f"Choose a whole number between {minimum} and {maximum}.", field)


def simulate(database, data, today):
    from .shared import recurring_cash_rules

    months = integer(data.get("months", 12), 1, 24, "months")
    reserve = money(data.get("reserve", "0"), "reserve")
    price = money(data.get("purchase_amount", "0"), "purchase_amount")
    rent = integer(data.get("rent_percent", 0), 0, 300, "rent_percent")
    rent_target = data.get("rent_rule")
    shared_rent = isinstance(rent_target, str) and rent_target.startswith("shared:")
    rent_rule = identifier(rent_target.removeprefix("shared:") if shared_rent else rent_target, "rent_rule")
    contribution = money(data.get("investment_delta", "0"), "investment_delta")
    if min(price, reserve) < 0:
        raise ApiError("Purchase and reserve amounts must be zero or more.")
    start = today
    end = month_end(month_start(start, months - 1))
    baseline_day = parse_date(data.get("purchase_on") or min(today + 30 * DAY, end).isoformat(), "purchase_on")
    delay = integer(data.get("purchase_shift", 0), -24, 24, "purchase_shift")
    alternative_day = add_months(baseline_day, delay)
    if price and (not start <= baseline_day <= end or not start <= alternative_day <= end):
        raise ApiError("Both purchase dates must fall within the forecast horizon.", "purchase_on")
    if rent:
        if shared_rent:
            eligible = any(rule["id"] == rent_rule for rule in recurring_cash_rules(database))
        else:
            with database.connection() as conn:
                rule = row_by_id(conn, "commitments", rent_rule, "rent_rule")
                eligible = rule["kind"] == "expense" and rule["cadence"] != "once" and not rule["archived_on"]
        if not eligible:
            raise ApiError("Choose an active recurring expense to apply the increase.", "rent_rule")
    opening, events, ids = cash_path(database, today, end)
    original, alternative = Counter(), Counter()
    reductions = Counter()
    for event in events:
        day, amount = event["date"], event["amount"]
        original[day] += amount
        is_target = event.get("rule_id") == rent_rule and bool(event.get("shared")) == shared_rent
        scaled = ((abs(amount) * rent + 50) // 100 if rent and is_target and event["kind"] == "expense" else 0)
        reduction = 0
        if contribution < 0 and event["kind"] == "investment" and amount < 0:
            month = month_start(day)
            reduction = min(-amount, -contribution - reductions[month])
            reductions[month] += reduction
        alternative[day] += amount - scaled + reduction
    if price:
        original[baseline_day] -= price
        alternative[alternative_day] -= price
    if contribution > 0:
        for offset in range(months):
            alternative[month_end(month_start(today, offset))] -= contribution
    base_cash = alt_cash = opening
    base_breach = alt_breach = today.isoformat() if opening < reserve else None
    points = [{"day": today.isoformat(), "month": today.isoformat()[:7], "baseline": str(opening),
               "alternative": str(opening), "opening": True}]
    for offset in range((end - today).days + 1):
        day = today + offset * DAY
        base_cash += original[day]
        alt_cash += alternative[day]
        if base_cash < reserve and base_breach is None:
            base_breach = day.isoformat()
        if alt_cash < reserve and alt_breach is None:
            alt_breach = day.isoformat()
        if day == month_end(day):
            points.append({"day": day.isoformat(), "month": day.isoformat()[:7], "baseline": str(base_cash),
                           "alternative": str(alt_cash)})
    return {
        "points": points, "reserve": str(reserve), "baseline_breach": base_breach,
        "alternative_breach": alt_breach, "purchase_on": baseline_day.isoformat() if price else None,
        "alternative_purchase_on": alternative_day.isoformat() if price else None,
        "account_scope": sorted(ids),
    }


def save_simulation(database, data, today):
    name = line(data.get("name"), "name", 80)
    inputs = data.get("inputs")
    if not isinstance(inputs, dict):
        raise ApiError("Enter inputs for the comparison.", "inputs")
    result = simulate(database, inputs, today)
    with database.connection(write=True) as conn:
        item_id = conn.execute("INSERT INTO forecast_comparisons(name, saved_on, inputs, result) VALUES(?, ?, ?, ?)",
                               (name, today.isoformat(), json.dumps(inputs), json.dumps(result))).lastrowid
    return {"id": item_id, "name": name, "inputs": inputs, "result": result}


def plan_metadata(database, data):
    raw = data.get("varianceTargets")
    ledger = database.ledger()
    categories = {item["id"]: item for item in ledger.categories}
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > 100:
        raise ApiError("Use up to 100 monthly category estimates.", "varianceTargets")
    targets = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ApiError("Choose a category and monthly amount.", "varianceTargets")
        key = identifier(item.get("category_id"), "varianceTargets")
        if key in targets or key not in categories:
            raise ApiError("Choose an existing category once per estimate.", "varianceTargets")
        amount = money(item.get("amount"), "varianceTargets")
        if amount < 0:
            raise ApiError("Monthly category estimates cannot be negative.", "varianceTargets")
        targets[key] = {"category_id": key, "amount": str(amount), "name": categories[key]["name"],
                        "kind": categories[key]["kind"]}
    return {"varianceTargets": list(targets.values()), "accountScope": sorted(liquid_ids(ledger))}


def actual_dates(ledger, points, scope, today):
    observed = [date.fromisoformat(point["day"]) for point in points if point["day"] <= today.isoformat()
                and not point.get("opening")]
    balances = historical_cash(ledger, [Window(day, day) for day in observed], scope)
    by_day = dict(zip((day.isoformat() for day in observed), balances))
    earliest = min((account["opened_on"] for account in ledger.accounts if account["id"] in scope), default=None)
    return [dict(point, actual=str(by_day[point["day"]]) if earliest and point["day"] >= earliest
                 and point["day"] in by_day and (not point.get("completed_only") or point["day"] < today.isoformat())
                 else None) for point in points]


def plan_actual(database, scenario, today, approach=None):
    ledger = database.ledger()
    inputs = scenario["input"]
    scope = set(inputs.get("accountScope", liquid_ids(ledger)))
    plans = scenario["result"]["plans"]
    plan = next((item for item in plans if item["approach"] == approach), None)
    plan = plan or (plans[0] if plans else None)
    rows = plan["projection"] if plan else []
    points = [{
        "month": row["month"],
        "day": month_end(date.fromisoformat(row["month"] + "-01")).isoformat(),
        "plan": str(int(row["availableCash"]) + int(row["earmarkedCash"])),
        "completed_only": True,
    } for row in rows]
    points = actual_dates(ledger, points, scope, today)
    categories = {item["id"]: item["name"] for item in ledger.categories}
    targets = {row["category_id"]: row for row in inputs.get("varianceTargets", [])}
    observed = points[1:]
    observed = [point for point in observed if point["actual"] is not None]
    observed_months = {point["month"] for point in observed}
    totals = defaultdict(Counter)
    for entry in ledger.entries:
        month = entry["posted_on"][:7]
        if month not in observed_months or entry["kind"] not in ("income", "expense", "investment"):
            continue
        if entry["account_id"] not in scope and not (entry["account_id"] is None and entry.get("shared")):
            continue
        totals[month][(entry["kind"], entry["category_id"])] += entry["amount"]
    def compare_categories(cells, months):
        keys = set(cells) | {(row["kind"], key) for key, row in targets.items()}
        detail = []
        for kind, key in keys:
            target = targets.get(key)
            actual = cells[(kind, key)]
            planned = int(target["amount"]) * months if target is not None else None
            impact = (actual - planned) * (1 if kind == "income" else -1) if planned is not None else None
            detail.append({
                "kind": kind, "name": target["name"] if target else categories.get(key, "Uncategorized"),
                "category_id": key, "planned": None if planned is None else str(planned),
                "actual": str(actual), "variance": None if impact is None else str(impact),
            })
        return sorted(detail, key=lambda row: (-abs(int(row["variance"] or 0)), row["kind"], row["name"]))

    categorized = {}
    cumulative = {}
    running = Counter()
    for count, month in enumerate(sorted(observed_months), 1):
        running.update(totals[month])
        categorized[month] = compare_categories(totals[month], 1)
        cumulative[month] = compare_categories(running, count)
    return {"points": points, "categories": categorized, "categories_cumulative": cumulative, "name": scenario["name"],
            "approach": plan["approach"] if plan else None, "approaches": [item["approach"] for item in plans],
            "has_targets": bool(targets), "account_scope": sorted(scope)}


def handle_forecast(handler, path, query, method, today):
    if not path.startswith("/api/forecast/"):
        return None
    resource = path.removeprefix("/api/forecast/")
    database = getattr(handler, "database", handler.server.database)
    item = re.fullmatch(r"(commitments|comparisons|actual|actual-simulation)/([1-9][0-9]{0,17})", resource)
    if method == "GET":
        if resource == "setup":
            from .shared import recurring_cash_rules
            with database.connection(snapshot=True) as conn:
                ledger = database.ledger(conn)
                rules, _ = rules_and_links(database, conn=conn)
                shared_rules = recurring_cash_rules(database, conn=conn)
            return {"today": today.isoformat(), "accounts": [
                {key: account[key] for key in ("id", "name", "kind", "opened_on", "profile_id") if key in account}
                for account in ledger.accounts
            ], "categories": ledger.categories, "rules": [listed_rule(rule) for rule in rules],
                "shared_rules": shared_rules}, 200
        if resource == "runway":
            value = calendar_month(query.get("month", today.isoformat()[:7]), "month")
            return runway(database, parse_date(value + "-01", "month"), today), 200
        if resource == "matches":
            due = parse_date(query.get("date"), "date")
            rule_id = identifier(query.get("rule_id"), "rule_id")
            with database.connection(snapshot=True) as conn:
                rule = row_by_id(conn, "commitments", rule_id)
                entries = conn.execute("""
                    SELECT id, name, posted_on, amount FROM entries
                    WHERE kind = ? AND account_id = ? AND destination_id IS ?
                    AND posted_on BETWEEN ? AND ?
                    AND NOT EXISTS (SELECT 1 FROM commitment_occurrences WHERE entry_id = entries.id)
                    ORDER BY posted_on DESC, id DESC LIMIT 50
                """, (rule["kind"], rule["account_id"], rule["destination_id"],
                      (due - 14 * DAY).isoformat(), min(today, due + 14 * DAY).isoformat()))
                return [dict(entry, amount=str(entry["amount"])) for entry in entries], 200
        if resource == "comparisons":
            with database.connection() as conn:
                return [dict(row) for row in conn.execute(
                    "SELECT id, name, saved_on FROM forecast_comparisons ORDER BY id DESC")], 200
        if item and item[1] == "actual":
            return plan_actual(database, database.scenario(int(item[2])), today, query.get("approach")), 200
        if item and item[1] in ("comparisons", "actual-simulation"):
            with database.connection() as conn:
                row = row_by_id(conn, "forecast_comparisons", int(item[2]))
                result = json.loads(row["result"])
                if item[1] == "actual-simulation":
                    points = [dict(point, completed_only=True) for point in result["points"]]
                    result["points"] = actual_dates(database.ledger(), points, set(result["account_scope"]), today)
                return dict(row, inputs=json.loads(row["inputs"]), result=result), 200
    if method == "POST":
        if resource == "simulate":
            return simulate(database, handler.read_data(16_384), today), 200
        if resource == "commitments":
            return {"id": save_rule(database, handler.read_data(16_384))}, 201
        if resource == "occurrences":
            return {"id": post_occurrence(database, handler.read_data(16_384), today)}, 200
        if resource == "comparisons":
            return save_simulation(database, handler.read_data(16_384), today), 201
    if method == "DELETE" and item:
        if item[1] == "commitments":
            archive_rule(database, int(item[2]), today)
            return {"id": int(item[2])}, 200
        if item[1] == "comparisons":
            with database.connection(write=True) as conn:
                row_by_id(conn, "forecast_comparisons", int(item[2]))
                conn.execute("DELETE FROM forecast_comparisons WHERE id = ?", (int(item[2]),))
                return {"id": int(item[2])}, 200
    raise ApiError("This forecast action was not found.", status=404)
