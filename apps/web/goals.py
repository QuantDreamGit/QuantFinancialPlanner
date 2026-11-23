"""Earmark savings and forecast how deposits and withdrawals affect goals."""
import re
from collections import defaultdict
from datetime import date

from .reports import DAY, Window, month_start, timeline
from .storage import row_by_id
from .values import ApiError, identifier, line, money
from .workspaces import owner_id


PROJECTION_MONTHS = 60
GOAL_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    target INTEGER NOT NULL CHECK (target > 0),
    target_on TEXT
);
CREATE TABLE IF NOT EXISTS goal_funding (
    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    earmarked INTEGER NOT NULL CHECK (earmarked >= 0),
    monthly INTEGER NOT NULL CHECK (monthly >= 0),
    PRIMARY KEY (goal_id, account_id)
);
CREATE INDEX IF NOT EXISTS goal_funding_account ON goal_funding(account_id);
COMMIT;
"""
GOAL_ROUTE = re.compile(r"/api/goals(?:/([1-9][0-9]{0,17}|redirect|preview))?")


def initialize_goals(database):
    with database.connection() as conn:
        conn.executescript(GOAL_SCHEMA)


def date_input(value):
    if value in (None, ""):
        return None
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value:
            return value
    except (ValueError, TypeError):
        pass
    raise ApiError("Enter a valid target date.", "target_on")


def read_funding(value):
    if not isinstance(value, list) or not value:
        raise ApiError("Link at least one savings account.", "funding")
    funding = {}
    for link in value:
        if not isinstance(link, dict):
            raise ApiError("Enter funding amounts for each savings account.", "funding")
        account_id = identifier(link.get("account_id"), "funding")
        if account_id in funding:
            raise ApiError("Select each savings account once per goal.", "funding")
        earmarked = money(link.get("earmarked"), "funding")
        monthly = money(link.get("monthly"), "funding")
        if min(earmarked, monthly) < 0:
            raise ApiError("Funding amounts must be zero or greater.", "funding")
        funding[account_id] = (earmarked, monthly)
    return funding


def save_goal(database, data, today, goal_id=None):
    name = line(data.get("name"), "name", 80)
    target = money(data.get("target"), "target")
    if target <= 0:
        raise ApiError("Enter a target amount greater than zero.", "target")
    target_on = date_input(data.get("target_on"))
    funding = read_funding(data.get("funding"))
    if sum(earmarked for earmarked, _ in funding.values()) > target:
        raise ApiError("Reduce the earmarked total to the goal target or below.", "funding")

    with database.connection(write=True) as conn:
        if goal_id is not None:
            row_by_id(conn, "goals", goal_id)
        if conn.execute("SELECT 1 FROM goals WHERE name = ? AND id <> ?", (name, goal_id or 0)).fetchone():
            raise ApiError("Choose a unique goal name.", "name")
        ledger = database.ledger(conn=conn)
        _, balances = timeline(ledger, [Window(today, today)])
        savings = {item["id"]: item for item in ledger.accounts if item["kind"] == "savings"}
        for account_id, (earmarked, _) in funding.items():
            account = savings.get(account_id)
            if not account:
                raise ApiError("Choose an existing savings account.", "funding")
            assignments = dict(conn.execute(
                "SELECT goal_id, earmarked FROM goal_funding WHERE account_id = ?", (account_id,),
            ))
            existing = assignments.pop(goal_id, 0)
            assigned_elsewhere = sum(assignments.values())
            capacity = max(0, balances.get(account_id, 0) - assigned_elsewhere)
            if earmarked > max(existing, capacity):
                raise ApiError(f"{account['name']} does not have enough unassigned cash for that earmark.", "funding")

        if goal_id is None:
            goal_id = conn.execute(
                "INSERT INTO goals(name, target, target_on) VALUES (?, ?, ?)", (name, target, target_on),
            ).lastrowid
        else:
            conn.execute("UPDATE goals SET name = ?, target = ?, target_on = ? WHERE id = ?", (name, target, target_on, goal_id))
            conn.execute("DELETE FROM goal_funding WHERE goal_id = ?", (goal_id,))
        conn.executemany(
            "INSERT INTO goal_funding(goal_id, account_id, earmarked, monthly) VALUES (?, ?, ?, ?)",
            [(goal_id, account, earmarked, monthly) for account, (earmarked, monthly) in funding.items()],
        )
    return goal_id


def load_goals(conn):
    by_goal = defaultdict(list)
    for row in conn.execute("SELECT * FROM goal_funding ORDER BY account_id"):
        by_goal[row["goal_id"]].append(dict(row))
    return [dict(row, funding=by_goal[row["id"]], profile_id=owner_id(row["id"]))
            for row in conn.execute("SELECT * FROM goals ORDER BY name, id")]


def allocate(capacity, claims):
    """Cover competing earmarks or requests proportionally, with exact cents."""
    total = sum(claims.values())
    if total <= capacity:
        return dict(claims)
    assigned = {}
    remainder = []
    for key, claim in claims.items():
        whole, residual = divmod(capacity * claim, total)
        assigned[key] = whole
        if residual:
            remainder.append((-residual, key))
    missing = capacity - sum(assigned.values())
    for _, key in sorted(remainder)[:missing]:
        assigned[key] += 1
    return assigned


def project_goals(goals, balances, today, projected_events=(), *, through=None, states=None):
    targets = {goal["id"]: goal["target"] for goal in goals}
    claims = defaultdict(dict)
    requests = defaultdict(dict)
    for goal in goals:
        for link in goal["funding"]:
            claims[link["account_id"]][goal["id"]] = link["earmarked"]
            requests[link["account_id"]][goal["id"]] = link["monthly"]

    initial_backing = {account: allocate(max(0, balances.get(account, 0)), amounts)
                       for account, amounts in claims.items()}
    balances = dict(balances)
    future_by_month = defaultdict(lambda: defaultdict(int))
    for event in projected_events:
        account = event["account_id"]
        day = event["date"]
        if account in claims and day >= today and (through is None or day <= through):
            future_by_month[month_start(day)][account] += event["amount"]

    def combined(by_account):
        totals = dict.fromkeys(targets, 0)
        for amounts in by_account.values():
            for goal_id, amount in amounts.items():
                totals[goal_id] += amount
        return totals

    current = combined(initial_backing)
    estimates = {key: today if amount >= targets[key] else None for key, amount in current.items()}
    projected = current
    if not targets or (states is None and through is None and all(estimates.values())):
        return current, initial_backing, estimates, projected

    for offset in range(PROJECTION_MONTHS + 1):
        month = month_start(today, offset)
        if through is not None and month > through:
            break
        end = month_start(month, 1) - DAY
        cutoff = min(end, through) if through is not None else end
        for account, change in future_by_month[month].items():
            balances[account] = balances.get(account, 0) + change

        backed = {account: allocate(max(0, balances.get(account, 0)), amounts)
                  for account, amounts in claims.items()}
        projected = combined(backed)
        contributions = defaultdict(int)
        if offset and cutoff == end:
            for account, account_claims in sorted(claims.items()):
                free_cash = max(0, balances.get(account, 0) - sum(account_claims.values()))
                if not free_cash:
                    continue
                installments = {
                    key: min(request, max(0, targets[key] - projected[key]))
                    for key, request in requests[account].items()
                }
                for key, amount in allocate(free_cash, installments).items():
                    account_claims[key] += amount
                    projected[key] += amount
                    contributions[key] += amount

        for key, amount in projected.items():
            if estimates[key] is None and amount >= targets[key]:
                estimates[key] = cutoff
        if states is not None:
            states[cutoff] = {
                "balances": dict(balances), "claims": {account: dict(assigned) for account, assigned in claims.items()},
                "contributions": dict(contributions),
            }
        if states is None and through is None and all(estimates.values()):
            break
    return current, initial_backing, estimates, projected


def monthly_allocation(rate, capacity_by_day, remaining):
    """Allocate a monthly cap across the linked accounts with durable headroom."""
    totals = defaultdict(int)
    schedule = {}
    for day, capacities in capacity_by_day.items():
        available = min(rate, remaining)
        month = {}
        for account_id, capacity in capacities.items():
            amount = min(available, max(0, capacity - totals[account_id]))
            if amount:
                month[account_id] = amount
                totals[account_id] += amount
                remaining -= amount
                available -= amount
        schedule[day] = month
    return remaining, schedule


def reserve_floor(ledger, balances, today, deadline, events, claims, states, extra=None):
    scope = {account["id"] for account in ledger.accounts if account["kind"] in ("checking", "cash", "savings")}
    balances = {key: value for key, value in balances.items() if key in scope}
    scheduled = defaultdict(lambda: defaultdict(int))
    for event in events:
        if event["account_id"] in scope and today <= event["date"] <= deadline:
            scheduled[event["date"]][event["account_id"]] += event["amount"]

    def unearmarked():
        return sum(balances.values()) - sum(
            min(max(balances.get(account, 0), 0), total + additional.get(account, 0))
            for account, total in earmarks.items()
        )

    earmarks = dict(claims)
    additional = defaultdict(int)
    low, low_on = unearmarked(), today
    for day in sorted(set(scheduled) | set(states)):
        for key, change in scheduled[day].items():
            balances[key] = balances.get(key, 0) + change
        if day in states:
            earmarks = {key: sum(levels.values()) for key, levels in states[day]["claims"].items()}
        if extra:
            for key, amount in extra.get(day, {}).items():
                additional[key] += amount
        available = unearmarked()
        if available < low:
            low, low_on = available, day
    return {"minimum": str(low), "on": low_on.isoformat()}


def contribution_assessment(goal, goals, ledger, balances, today, events, funded, reserve=None):
    key = goal["id"]
    target = goal["target"]
    links = {link["account_id"]: link for link in goal["funding"]}
    total_claims = defaultdict(int)
    competing_rate = 0
    for item in goals:
        for link in item["funding"]:
            total_claims[link["account_id"]] += link["earmarked"]
            if item["id"] != key and link["account_id"] in links:
                competing_rate += link["monthly"]
    configured_rate = sum(link["monthly"] for link in links.values())
    result = {
        "status": "deadline_missing", "remaining": str(max(0, target - funded)),
        "configured_monthly": str(configured_rate), "competing_monthly": str(competing_rate),
        "unassigned_today": str(sum(max(0, balances.get(account, 0) - total_claims[account]) for account in links)),
        "reserve": str(reserve) if reserve is not None else None,
    }
    if goal["target_on"] is None:
        if funded >= target:
            result["status"] = "funded"
        return result
    deadline = date.fromisoformat(goal["target_on"])
    if deadline < today:
        return dict(result, status="funded" if funded >= target else "deadline_passed")
    if deadline >= month_start(today, PROJECTION_MONTHS + 1):
        return dict(result, status="funded" if funded >= target else "outside_horizon")

    # Project competitors with their scheduled amounts and preserve the target’s existing earmarks.
    protected_goals = [
        dict(item, funding=[dict(link, monthly=0) for link in item["funding"]]) if item["id"] == key else item
        for item in goals
    ]
    protected_states = {}
    project_goals(protected_goals, balances, today, events, through=deadline, states=protected_states)

    def capacity(state):
        return {account: max(0, state["balances"][account] - sum(
            value for goal_id, value in state["claims"][account].items() if goal_id != key
        )) for account in links}

    terminal_capacity = capacity(protected_states[deadline])
    stable = {account: min(level, links[account]["earmarked"]) for account, level in terminal_capacity.items()}
    remaining = max(0, target - sum(stable.values()))
    capacity_by_day = {}
    durable = dict(terminal_capacity)
    for day, state in reversed(protected_states.items()):
        for account, level in capacity(state).items():
            durable[account] = min(durable[account], level)
        if month_start(day) == month_start(today) or day != month_start(day, 1) - DAY:
            continue
        capacity_by_day[day] = {
            account: max(0, level - links[account]["earmarked"]) for account, level in durable.items()
        }
    capacity_by_day = dict(sorted(capacity_by_day.items()))
    slots = len(capacity_by_day)
    baseline_states = {}
    configured_end = project_goals(goals, balances, today, events, through=deadline, states=baseline_states)[3][key]
    max_gap, _ = monthly_allocation(remaining, capacity_by_day, remaining)
    result.update({
        "installments": slots, "configured_end": str(configured_end),
        "scheduled_net": str(sum(event["amount"] for event in events
                                 if event["account_id"] in links and today <= event["date"] <= deadline)),
        "protected_end": str(target - max_gap), "additional_needed": str(max_gap),
        "retained_earmarks": str(sum(stable.values())),
        "straight_line": str((remaining + slots - 1) // slots) if slots else None,
        "first_configured": str(baseline_states[next(iter(capacity_by_day))]["contributions"].get(key, 0)) if slots else None,
        "suggested_monthly": None, "suggested_schedule": [],
    })
    if reserve is not None:
        result["configured_reserve"] = reserve_floor(ledger, balances, today, deadline, events, total_claims, baseline_states)

    if max_gap:
        result["status"] = "competition" if configured_end >= target else ("funding_gap" if slots else "deadline_too_soon")
        return result

    low, high = 0, remaining
    while low < high:
        mid = (low + high) // 2
        gap, _ = monthly_allocation(mid, capacity_by_day, remaining)
        if gap:
            low = mid + 1
        else:
            high = mid
    _, schedule = monthly_allocation(low, capacity_by_day, remaining)
    result.update({
        "suggested_monthly": str(low),
        "suggested_schedule": [
            {"on": day.isoformat(), "links": [{"account_id": account, "amount": str(amount)} for account, amount in allocations.items()]}
            for day, allocations in schedule.items() if allocations
        ],
    })
    reserve_breach = False
    if reserve is not None:
        floor = reserve_floor(ledger, balances, today, deadline, events, total_claims, protected_states, schedule)
        result["suggested_reserve"] = floor
        reserve_breach = int(floor["minimum"]) < reserve or int(result["configured_reserve"]["minimum"]) < reserve
    result["status"] = ("risky" if reserve_breach else "funded" if funded >= target and low == 0
                        else "on_track" if configured_end >= target else "increased_pace")
    return result


def report(database, today, redirect=None, *, reserve=None):
    from .forecast import projected_events

    with database.connection(snapshot=True) as conn:
        ledger = database.ledger(conn=conn)
        goals = load_goals(conn)
        events = projected_events(
            database, today, month_start(today, PROJECTION_MONTHS + 1) - DAY, conn=conn,
        ) if goals else []
    return prepare_report(ledger, goals, today, events, redirect=redirect, reserve=reserve)


def prepare_report(ledger, goals, today, events, redirect=None, *, reserve=None):
    _, balances = timeline(ledger, [Window(today, today)])
    current, backing, estimates, projected = project_goals(goals, balances, today, events)
    if redirect:
        goals = [dict(goal, funding=[dict(link) for link in goal["funding"]]) for goal in goals]
        source, destination, amount = validate_redirect(
            {goal["id"]: {link["account_id"]: link for link in goal["funding"]} for goal in goals}, redirect,
        )
        source["monthly"] -= amount
        destination["monthly"] += amount
        _, _, new_estimates, new_projected = project_goals(goals, balances, today, events)
    else:
        new_estimates, new_projected = estimates, projected

    events_by_account = defaultdict(lambda: defaultdict(int))
    for event in events:
        if event["date"] >= today:
            events_by_account[event["account_id"]][event["date"]] += event["amount"]
    accounts = []
    for account in ledger.accounts:
        if account["kind"] != "savings":
            continue
        ident = account["id"]
        balance = balances.get(ident, 0)
        earmarked = sum(link["earmarked"] for goal in goals for link in goal["funding"] if link["account_id"] == ident)
        projected_balance = balance
        first_pressure_on = None
        if earmarked:
            for day, change in sorted(events_by_account[ident].items()):
                projected_balance += change
                if change < 0 and projected_balance < earmarked:
                    first_pressure_on = day.isoformat()
                    break
        item = {key: account[key] for key in ("id", "name")}
        accounts.append(dict(item, balance=str(balance), earmarked=str(earmarked),
                             unassigned=str(max(0, balance - earmarked)),
                             first_pressure_on=first_pressure_on))
    payload = []
    for goal in goals:
        key = goal["id"]
        payload.append(dict(
            goal, target=str(goal["target"]), funded=str(current[key]),
            estimated_on=new_estimates[key].isoformat() if new_estimates[key] else None,
            before_on=estimates[key].isoformat() if estimates[key] else None,
            projected_funded=str(new_projected[key]),
            before_projected_funded=str(projected[key]),
            recommendation=contribution_assessment(goal, goals, ledger, balances, today, events, current[key], reserve)
            if redirect is None else None,
            funding=[dict(link, earmarked=str(link["earmarked"]), monthly=str(link["monthly"]),
                          backed=str(backing[link["account_id"]][key])) for link in goal["funding"]],
        ))
    return {"accounts": accounts, "goals": payload, "today": today.isoformat(), "projection_months": PROJECTION_MONTHS}


def redirect_input(data):
    return {
        "from_id": identifier(data.get("from_id"), "from_id"),
        "to_id": identifier(data.get("to_id"), "to_id"),
        "account_id": identifier(data.get("account_id"), "account_id"),
        "amount": money(data.get("amount"), "amount"),
        "expected_from": money(data.get("expected_from"), "amount") if data.get("expected_from") is not None else None,
        "expected_to": money(data.get("expected_to"), "amount") if data.get("expected_to") is not None else None,
    }


def validate_redirect(links, data):
    account = data["account_id"]
    source = links.get(data["from_id"], {}).get(account)
    destination = links.get(data["to_id"], {}).get(account)
    if source is None or destination is None or source == destination:
        raise ApiError("Select two different goals that use the selected savings account.", "to_id")
    for expected, link in ((data["expected_from"], source), (data["expected_to"], destination)):
        if expected is not None and expected != link["monthly"]:
            raise ApiError("The monthly amounts changed. Reload the goals and preview the redirect again.", status=409)
    amount = data["amount"]
    if not 0 < amount <= source["monthly"] or destination["monthly"] + amount >= 2**63:
        raise ApiError("Enter a positive amount within the source goal’s monthly allocation.", "amount")
    return source, destination, amount


def save_redirect(database, data):
    if owner_id(data["from_id"]) or owner_id(data["to_id"]):
        raise ApiError("Redirect the contribution from the personal workspace that owns the goal.", "amount", 409)
    if data["expected_from"] is None or data["expected_to"] is None:
        raise ApiError("Preview the redirect before saving it.", status=409)
    with database.connection(write=True) as conn:
        links = {
            goal["id"]: {link["account_id"]: link for link in goal["funding"]}
            for goal in load_goals(conn)
        }
        source, destination, amount = validate_redirect(links, data)
        updates = ((source["monthly"] - amount, source["goal_id"], source["account_id"]),
                   (destination["monthly"] + amount, destination["goal_id"], destination["account_id"]))
        conn.executemany("UPDATE goal_funding SET monthly = ? WHERE goal_id = ? AND account_id = ?", updates)


def handle_goals(handler, path, query, method, today):
    match = GOAL_ROUTE.fullmatch(path)
    if not match:
        return None
    database = getattr(handler, "database", handler.server.database)
    arg = match[1]
    if method == "GET" and arg is None:
        reserve = money(query["reserve"], "reserve") if query.get("reserve") else None
        if reserve is not None and reserve < 0:
            raise ApiError("Enter a reserve of zero or more.", "reserve")
        return report(database, today, reserve=reserve), 200
    if method == "POST" and arg in ("preview", "redirect"):
        data = redirect_input(handler.read_data(8192))
        if arg == "preview":
            return report(database, today, data), 200
        save_redirect(database, data)
        return {"saved": True}, 200
    goal_id = int(arg) if arg and arg.isdigit() else None
    if (method == "POST" and arg is None) or (method == "PUT" and goal_id is not None):
        goal_id = save_goal(database, handler.read_data(16384), today, goal_id)
        return {"id": goal_id}, 201 if method == "POST" else 200
    if method == "DELETE" and goal_id is not None:
        with database.connection(write=True) as conn:
            row_by_id(conn, "goals", goal_id)
            conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
        return {"id": goal_id}, 200
    raise ApiError("This request method is unavailable.", status=405)
