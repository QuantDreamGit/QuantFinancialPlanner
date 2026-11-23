from collections import Counter, defaultdict
from datetime import date
from statistics import median_low, median_high

from .planning import calendar_month
from .reports import DAY, Window, category_totals, entry_payload, history_start, month_start, timeline
from .storage import FLOW_KINDS
from .values import ApiError


def month_window(value, today):
    start = date.fromisoformat(calendar_month(value or today.isoformat()[:7], "month") + "-01")
    if start > today:
        raise ApiError("Choose the current month or a past month.", "month")
    end = today if start == month_start(today) else month_start(start, 1) - DAY
    return Window(start, end)


def replay(ledger, window):
    entries = [entry for entry in ledger.entries if window.includes(entry)]
    buckets = Counter()
    amounts = Counter()
    counts = Counter()
    for entry in ledger.entries:
        kind = entry["kind"]
        if kind in FLOW_KINDS:
            buckets[entry["posted_on"][:7], kind] += entry["amount"]
    for entry in entries:
        amounts[entry["kind"]] += entry["amount"]
        counts[entry["kind"]] += 1
    categories, unassigned = category_totals(ledger, entries)
    first = history_start(ledger, window.end).isoformat()
    return {
        "month": window.start.isoformat()[:7], "through": window.end.isoformat(), "first": first[:7],
        "scale": str(max(buckets.values(), default=0)),
        "summary": {kind: str(amounts[kind]) for kind in FLOW_KINDS},
        "flow_counts": {kind: counts[kind] for kind in FLOW_KINDS},
        "categories": categories, "unassigned": unassigned,
    }


def waterfall(ledger, window):
    previous = window.start - DAY if window.start > date.min else None
    spans = ([Window(previous, previous)] if previous else []) + [window]
    points, _ = timeline(ledger, spans)
    start = int(points[0]["wealth"] or 0) if previous else 0
    end = int(points[-1]["wealth"] or 0)
    details = defaultdict(list)
    changes = Counter()
    for account in ledger.accounts:
        if window.start.isoformat() <= account["opened_on"] <= window.end.isoformat():
            details["openings"].append({
                "name": account["name"], "posted_on": account["opened_on"],
                "amount": str(account["opening_balance"]),
            })
            changes["openings"] += account["opening_balance"]
    steps = {"income": "Income", "expense": "Expenses", "valuation": "Investment value change",
             "openings": "Opening balances added"}
    for entry in ledger.entries:
        kind = entry["kind"]
        if kind in steps and window.includes(entry):
            sign = -1 if kind == "expense" else 1
            changes[kind] += sign * entry["amount"]
            details[kind].append(entry_payload(entry))
    rows = [{"key": "start", "label": "Opening net worth", "value": str(start), "from": "0", "to": str(start), "count": 0}]
    running = start
    for key, label in steps.items():
        if key == "openings" and not details[key]:
            continue
        next_total = running + changes[key]
        rows.append({"key": key, "label": label, "value": str(changes[key]), "from": str(running),
                     "to": str(next_total), "count": len(details[key])})
        running = next_total
    if running != end:
        raise RuntimeError("Net worth and ledger changes do not reconcile")
    rows.append({"key": "end", "label": "Closing net worth", "value": str(end), "from": "0", "to": str(end), "count": 0})
    return rows, details


CADENCES = {"weekly": (6, 8), "monthly": (26, 34), "quarterly": (84, 101), "yearly": (350, 380)}


def recurrence(entries):
    if len(entries) < 3:
        return None, []
    dates = [date.fromisoformat(entry["posted_on"]) for entry in entries]
    recent = (dates[-1] - dates[-2]).days
    for cadence, (low, high) in CADENCES.items():
        if not low <= recent <= high:
            continue
        start = len(dates) - 2
        while start and low <= (dates[start] - dates[start - 1]).days <= high:
            start -= 1
        if len(dates) - start >= 3:
            return cadence, entries[start:]
    return None, []


def median_cents(values):
    return (median_low(values) + median_high(values)) // 2


def charge_price(entry):
    return int(entry["shared"]["gross"]) if entry.get("shared") else entry["amount"]


def detective(ledger, window):
    merchants = defaultdict(list)
    by_category = defaultdict(list)
    category_names = {item["id"]: item["name"] for item in ledger.categories}
    for entry in ledger.entries:
        if entry["kind"] != "expense" or entry["posted_on"] > window.end.isoformat():
            continue
        category = entry["category_id"]
        merchant = " ".join(entry["name"].casefold().split())
        rule = entry.get("shared", {}).get("rule_id")
        merchants[merchant, category, rule].append(entry)
        by_category[category].append(entry)

    flags = []

    def flag(kind, key, label, recent, history, amount, baseline, note, full_bill=False):
        flags.append({
            "id": f"{kind}:{key}", "kind": kind, "label": label,
            "amount": str(amount), "baseline": str(baseline) if baseline is not None else None,
            "detail": note, "recent": recent, "history": history, "full_bill": full_bill,
        })

    for entries in merchants.values():
        if not window.includes(entries[-1]):
            continue
        recent = entries[-1]
        cadence, series = recurrence(entries)
        if not series:
            continue
        if series[0]["id"] == entries[0]["id"] and window.includes(series[2]):
            flag("new", recent["id"], recent["name"], series[2:], series[:2], series[2]["amount"], None,
                 f"{cadence.capitalize()} pattern detected after three payments")
        history = series[-7:-1]
        typical = median_cents([charge_price(item) for item in history])
        price = charge_price(recent)
        if price - typical >= 100 and price * 100 >= typical * 105:
            basis = " full bills" if recent.get("shared") else " payments"
            flag("price", recent["id"], recent["name"], [recent], history, price, typical,
                 f"Median of {len(history)} previous {cadence}{basis}", full_bill=bool(recent.get("shared")))

    first = history_start(ledger, window.start).isoformat()
    full_month = window.end != date.max and (window.end + DAY).day == 1
    # Match the same days when the selected month is still in progress.
    if window.start.year * 12 + window.start.month > 15:
        comparison = []
        for offset in range(-3, 0):
            start = month_start(window.start, offset)
            end = month_start(start, 1) - DAY
            if not full_month:
                end = min(end, start + (window.end.day - 1) * DAY)
            comparison.append(Window(start, end))
        if first <= comparison[0].start.isoformat():
            for category, entries in by_category.items():
                recent = [item for item in entries if window.includes(item)]
                if not recent:
                    continue
                historical = [[item for item in entries if span.includes(item)] for span in comparison]
                typical = median_cents([sum(item["amount"] for item in group) for group in historical])
                current = sum(item["amount"] for item in recent)
                if typical and current - typical >= 2500 and current * 100 >= typical * 130:
                    suffix = "completed months" if full_month else f"matching first {window.end.day} days"
                    flag("category", category or 0, category_names.get(category, "Uncategorized"), recent,
                         [item for group in historical for item in group], current, typical,
                         f"Median across three prior months ({suffix})")
    return sorted(flags, key=lambda row: int(row["amount"]) - int(row["baseline"] or 0), reverse=True)


def handle_insights(path, query, ledger, today):
    window = month_window(query.get("month"), today)
    if path == "/api/insights/replay":
        return replay(ledger, window)
    page = max(1, int(query.get("page", 1)))
    size = 25

    def paginate(rows):
        rows = sorted(rows, key=lambda row: row["posted_on"], reverse=True)
        start = (page - 1) * size
        return {"rows": rows[start:start + size], "total": len(rows), "page": page, "size": size}

    if path == "/api/insights/waterfall":
        rows, details = waterfall(ledger, window)
        if "step" in query:
            return paginate(details[query["step"]])
        return {"steps": rows, "from": window.start.isoformat(), "to": window.end.isoformat()}
    flags = detective(ledger, window)
    if "flag" in query:
        flag = next((row for row in flags if row["id"] == query["flag"]), None)
        if not flag:
            raise ApiError("This spending flag is no longer available.", status=404)
        rows = flag["history" if query.get("prior") == "1" else "recent"]
        return paginate([dict(entry_payload(entry), amount=str(charge_price(entry))) if flag["full_bill"]
                         else entry_payload(entry) for entry in rows])
    return {"flags": [dict(row, recent=len(row["recent"]), history=len(row["history"])) for row in flags]}
