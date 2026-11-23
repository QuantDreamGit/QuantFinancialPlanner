from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from .storage import FLOW_KINDS, Ledger
from .values import euros


PERIOD_MONTHS = {"3M": 3, "6M": 6, "1Y": 12}
PERIODS = ("1M", *PERIOD_MONTHS, "ALL")
BALANCES = ("cash", "savings", "investments")
DAY = timedelta(days=1)


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    def includes(self, entry):
        return self.start.isoformat() <= entry["posted_on"] <= self.end.isoformat()


def month_start(day, offset=0):
    year, zero_month = divmod(day.year * 12 + day.month - 1 + offset, 12)
    return date(year, zero_month + 1, 1)


def selected_window(ledger, period, today):
    if period == "1M":
        return Window(today - 29 * DAY, today)
    if period == "ALL":
        return Window(history_start(ledger, today), today)
    return Window(month_start(today, 1 - PERIOD_MONTHS[period]), today)


def history_start(ledger, today):
    openings = [account["opened_on"] for account in ledger.accounts]
    if ledger.entries:
        openings.append(ledger.entries[0]["posted_on"])
    return date.fromisoformat(min(openings, default=today.isoformat()))


def previous_window(period, window):
    if period == "1M":
        return Window(window.start - 30 * DAY, window.start - DAY)
    if period == "ALL":
        return None
    months = PERIOD_MONTHS[period]
    end_month = month_start(window.end, -months)
    end = min(end_month + (window.end.day - 1) * DAY, month_start(end_month, 1) - DAY)
    return Window(month_start(window.start, -months), end)


def buckets(window, period):
    if period == "1M":
        return "day", [Window(window.start + offset * DAY, window.start + offset * DAY)
                       for offset in range((window.end - window.start).days + 1)]
    first = month_start(window.start)
    month_count = (window.end.year - first.year) * 12 + window.end.month - first.month + 1
    stride = max(1, (month_count + 59) // 60)
    boundaries = [month_start(first, offset) for offset in range(0, month_count, stride)]
    ends = [boundary - DAY for boundary in boundaries[1:]] + [window.end]
    return ("month" if stride == 1 else "span",
            [Window(max(start, window.start), end) for start, end in zip(boundaries, ends)])


def balance_group(account):
    return {"savings": "savings", "investment": "investments"}.get(account["kind"], "cash")


def movements(entry):
    if "cash_delta" in entry:
        if entry["account_id"] is not None:
            yield entry["account_id"], entry["cash_delta"]
        return
    amount = entry["amount"] if entry["kind"] in ("income", "valuation") else -entry["amount"]
    yield entry["account_id"], amount
    if entry["destination_id"] is not None:
        yield entry["destination_id"], entry["amount"]


def balance_totals(balances, accounts):
    totals = dict.fromkeys(BALANCES, 0)
    for account_id, balance in balances.items():
        totals[balance_group(accounts[account_id])] += balance
    return dict(totals, wealth=sum(totals.values()))


def timeline(ledger, spans):
    accounts = {account["id"]: account for account in ledger.accounts}
    openings = sorted(ledger.accounts, key=lambda account: account["opened_on"])
    open_index = entry_index = 0
    shared_balance = 0
    shared_activity = False
    balances = {}
    points = []
    for span in spans:
        start, end = span.start.isoformat(), span.end.isoformat()
        while open_index < len(openings) and openings[open_index]["opened_on"] <= end:
            account = openings[open_index]
            balances[account["id"]] = account["opening_balance"]
            open_index += 1
        flows = dict.fromkeys(FLOW_KINDS, 0)
        while entry_index < len(ledger.entries) and ledger.entries[entry_index]["posted_on"] <= end:
            entry = ledger.entries[entry_index]
            for account_id, change in movements(entry):
                balances[account_id] += change
            if "shared_delta" in entry:
                shared_balance += entry["shared_delta"]
                shared_activity = True
            if entry["posted_on"] >= start and entry["kind"] in flows:
                flows[entry["kind"]] += entry["amount"]
            entry_index += 1
        wealth = balance_totals(balances, accounts)
        wealth["wealth"] += shared_balance
        if shared_activity:
            wealth["shared_balance"] = shared_balance
        points.append({
            "from": start, "to": end,
            **{key: str(value) if balances or shared_activity else None for key, value in wealth.items()},
            **{key: str(value) for key, value in flows.items()},
        })
    return points, balances


def entry_payload(entry):
    return {key: str(value) if key in ("amount", "cash_delta", "shared_delta") else value
            for key, value in entry.items()}


def category_totals(ledger, entries):
    direct = Counter()
    direct_count = Counter()
    totals = Counter()
    counts = Counter()
    parents = {category["id"]: category["parent_id"] for category in ledger.categories}
    unassigned = {kind: {"amount": 0, "count": 0} for kind in FLOW_KINDS}
    for entry in entries:
        kind = entry["kind"]
        if kind not in unassigned:
            continue
        category = entry["category_id"]
        if category is None:
            unassigned[kind]["amount"] += entry["amount"]
            unassigned[kind]["count"] += 1
            continue
        direct[category] += entry["amount"]
        direct_count[category] += 1
        while category is not None:
            totals[category] += entry["amount"]
            counts[category] += 1
            category = parents[category]
    categories = [
        dict(category, amount=str(totals[category["id"]]), count=counts[category["id"]],
             direct_amount=str(direct[category["id"]]), direct_count=direct_count[category["id"]])
        for category in ledger.categories
    ]
    return categories, {kind: dict(item, amount=str(item["amount"])) for kind, item in unassigned.items()}


def defaults_for(entries):
    defaults = {}
    labels = {}
    for entry in reversed(sorted(entries, key=lambda item: item["id"])):
        if entry["id"] > 0 and entry["kind"] not in defaults:
            defaults[entry["kind"]] = {key: entry[key] for key in ("account_id", "destination_id", "category_id", "labels")}
        for label in entry["labels"]:
            labels.setdefault(label.casefold(), label)
    return defaults, list(labels.values())[:80]


def dashboard(ledger: Ledger, period, today):
    window = selected_window(ledger, period, today)
    frequency, spans = buckets(window, period)
    points, balances = timeline(ledger, spans)
    baseline = previous_window(period, window)
    previous, _ = timeline(ledger, [baseline]) if baseline else ([], {})
    entries = [entry for entry in ledger.entries if window.includes(entry)]
    flows = {kind: str(sum(int(point[kind]) for point in points)) for kind in FLOW_KINDS}
    counts = Counter(entry["kind"] for entry in entries)
    categories, unassigned = category_totals(ledger, entries)
    defaults, labels = defaults_for(ledger.entries)
    return {
        "today": today.isoformat(),
        "from": window.start.isoformat(),
        "to": window.end.isoformat(),
        "frequency": frequency,
        "summary": {key: points[-1][key] for key in (*BALANCES, "wealth")} | flows,
        "shared_balance": points[-1].get("shared_balance"),
        "previous": previous[0] if previous else None,
        "points": points,
        "accounts": [
            dict(account, opening_balance=str(account["opening_balance"]), balance=str(balances.get(account["id"], 0)))
            for account in ledger.accounts
        ],
        "categories": categories,
        "unassigned": unassigned,
        "flow_counts": {kind: counts[kind] for kind in FLOW_KINDS},
        "recent": [entry_payload(entry) for entry in ledger.entries[:-7:-1]],
        "defaults": defaults,
        "labels": labels,
        "entry_count": len(ledger.entries),
    }


def transaction_list(ledger, *, page, size, search="", kind=None, category=None, account=None, window=None,
                     descendants=False):
    accounts = {item["id"]: item["name"] for item in ledger.accounts}
    categories = {item["id"]: item["name"] for item in ledger.categories}
    tokens = search.casefold().split()
    category_ids = {category} if isinstance(category, int) else set()
    if category_ids and descendants:
        children = {}
        for item in ledger.categories:
            children.setdefault(item["parent_id"], []).append(item["id"])
        pending = [category]
        while pending:
            nested = children.get(pending.pop(), [])
            category_ids.update(nested)
            pending.extend(nested)
    entries = []
    for entry in reversed(ledger.entries):
        if kind and entry["kind"] != kind:
            continue
        if category == "uncategorized" and entry["category_id"] is not None:
            continue
        if category_ids and entry["category_id"] not in category_ids:
            continue
        if account and account not in (entry["account_id"], entry["destination_id"]):
            continue
        if window and not window.includes(entry):
            continue
        haystack = " ".join([
            entry["name"], entry["note"], *entry["labels"], accounts.get(entry["account_id"], ""),
            accounts.get(entry["destination_id"], ""), categories.get(entry["category_id"], ""),
        ]).casefold()
        if all(token in haystack for token in tokens):
            entries.append(entry)
    start = (page - 1) * size
    return {"total": len(entries), "entries": [entry_payload(entry) for entry in entries[start:start + size]]}


def planner_inputs(ledger, today):
    current_month = month_start(today)
    lookback = Window(month_start(today, -3), current_month - DAY)
    _, balances = timeline(ledger, [Window(today, today)])
    liquid = {account["id"] for account in ledger.accounts if account["kind"] != "investment"}
    historical_flow = sum(
        change for entry in ledger.entries if lookback.includes(entry)
        for account_id, change in movements(entry) if account_id in liquid
    )
    # Round to the nearest cent while keeping the computation in integer cents.
    average = (abs(historical_flow) + 1) // 3 * (-1 if historical_flow < 0 else 1)
    return {
        "startingMonth": current_month.isoformat()[:7],
        "startingCash": euros(sum(balances.get(account, 0) for account in liquid)),
        "monthlyNetCashFlow": euros(average),
    }
