"""Month-by-month spending limits and funding commitments for a family workspace."""
import re
from collections import Counter, defaultdict
from datetime import date

from .forecast import month_end
from .planning import calendar_month
from .values import ApiError, identifier, line, money


SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS family_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    member_id INTEGER,
    category_id INTEGER,
    monthly_limit INTEGER NOT NULL CHECK (monthly_limit > 0),
    starts_month TEXT NOT NULL,
    ends_month TEXT,
    CHECK (ends_month IS NULL OR ends_month >= starts_month)
);
CREATE INDEX IF NOT EXISTS family_limits_scope
    ON family_limits(group_id, member_id, category_id, starts_month);
COMMIT;
"""
BASE = "/api/budgets"


def initialize_budgets(database):
    with database.connection() as conn:
        conn.executescript(SCHEMA)


def active_workspace(workspace):
    if not workspace or workspace.get("kind") != "group":
        raise ApiError("Switch to a family workspace to use group spending limits.", status=403)
    return workspace


def members_by_id(workspace):
    return {member["id"]: member["name"] for member in workspace["members"]}


def month_value(value, field="month"):
    month = calendar_month(value, field)
    if not "1900-01" <= month <= "9998-12":
        raise ApiError("Enter a calendar month between 1900 and 9998.", field)
    return month


def member_value(raw, members):
    member_id = identifier(raw, "member_id")
    if member_id is not None and member_id not in members:
        raise ApiError("Choose a current member of this family.", "member_id")
    return member_id


def expense_categories(ledger):
    return {item["id"]: item for item in ledger.categories if item["kind"] == "expense"}


def descendants(categories, category_id):
    if category_id is None:
        return None
    children = defaultdict(list)
    for child in categories.values():
        children[child.get("parent_id")].append(child["id"])
    found = set()
    pending = [category_id]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(children[current])
    return found


def budget_row(conn, table, group_id, item_id):
    row = conn.execute(f"SELECT * FROM {table} WHERE group_id = ? AND id = ?", (group_id, item_id)).fetchone()
    if row is None:
        raise ApiError("This item no longer exists in this family.", status=404)
    return row


def write_limit(database, workspace, data, item_id=None):
    group_id = active_workspace(workspace)["id"]
    members = members_by_id(workspace)
    label = line(data.get("name"), "name", 80)
    member_id = member_value(data.get("member_id"), members)
    category_id = identifier(data.get("category_id"), "category_id")
    amount = money(data.get("monthly_limit"), "monthly_limit")
    if amount <= 0:
        raise ApiError("Enter a monthly limit above €0.", "monthly_limit")
    starts = month_value(data.get("starts_month"), "starts_month")
    ends = month_value(data["ends_month"], "ends_month") if data.get("ends_month") else None
    if ends and ends < starts:
        raise ApiError("The last month must be on or after the first month.", "ends_month")
    with database.connection(write=True) as conn:
        if item_id is not None:
            budget_row(conn, "family_limits", group_id, item_id)
        if category_id is not None and category_id not in expense_categories(database.ledger(conn=conn)):
            raise ApiError("Choose an expense category in this family.", "category_id")
        if conn.execute("""
            SELECT 1 FROM family_limits
            WHERE group_id = ? AND member_id IS ? AND category_id IS ? AND id <> ?
                AND (ends_month IS NULL OR ends_month >= ?) AND (? IS NULL OR starts_month <= ?)
        """, (group_id, member_id, category_id, item_id or 0, starts, ends, ends)).fetchone():
            raise ApiError("A limit already covers this spending scope in those months.", "starts_month", status=409)
        values = (label, member_id, category_id, amount, starts, ends)
        if item_id is None:
            return conn.execute("""
                INSERT INTO family_limits (group_id, name, member_id, category_id, monthly_limit, starts_month, ends_month)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (group_id, *values)).lastrowid
        conn.execute("""
            UPDATE family_limits SET name = ?, member_id = ?, category_id = ?, monthly_limit = ?,
                starts_month = ?, ends_month = ? WHERE group_id = ? AND id = ?
        """, (*values, group_id, item_id))
        return item_id


def end_limit(database, workspace, item_id, last_month):
    group_id = active_workspace(workspace)["id"]
    month_value(last_month, "ends_month")
    with database.connection(write=True) as conn:
        prior = budget_row(conn, "family_limits", group_id, item_id)
        if last_month < prior["starts_month"]:
            conn.execute("DELETE FROM family_limits WHERE group_id = ? AND id = ?", (group_id, item_id))
        elif not prior["ends_month"] or last_month < prior["ends_month"]:
            conn.execute("UPDATE family_limits SET ends_month = ? WHERE group_id = ? AND id = ?",
                         (last_month, group_id, item_id))


def from_member(entry, members):
    owner = entry.get("profile_id")
    if owner in members:
        return owner
    return None


def report_limit(row, expenses, categories, members, projected):
    ceiling = row["monthly_limit"]
    included = descendants(categories, row["category_id"])

    def qualifies(item):
        return ((row["member_id"] is None or row["member_id"] == item["member_id"])
                and (included is None or item["category_id"] in included))

    matching = [entry for entry in expenses if qualifies(entry)]
    actual = sum(entry["amount"] for entry in matching)
    pending = sum(entry["amount"] for entry in projected if qualifies(entry))
    breakdown = Counter()
    for entry in matching:
        breakdown[entry["member_id"]] += entry["amount"]

    return dict(row, monthly_limit=str(ceiling), spent=str(actual), remaining=str(ceiling - actual),
                scheduled=str(pending), projected=str(actual + pending),
                exceeded=actual > ceiling,
                projected_exceeded=actual <= ceiling < actual + pending,
                category_name=categories.get(row["category_id"], {}).get("name"),
                members=[{"id": key, "name": members.get(key, "Group ledger"), "amount": str(value)}
                         for key, value in sorted(breakdown.items(), key=lambda item: (-item[1], item[0] or 0))],
                entries=[dict(item, amount=str(item["amount"])) for item in matching])


def expense_rows(ledger, members, start, end):
    start, end = start.isoformat(), end.isoformat()
    return [
        {"id": entry["id"], "name": entry["name"], "amount": entry["amount"], "posted_on": entry["posted_on"],
         "category_id": entry["category_id"], "member_id": from_member(entry, members)}
        for entry in reversed(ledger.entries)
        if entry["kind"] == "expense" and start <= entry["posted_on"] <= end
    ]


def future_expenses(database, members, start, end, account_owners, conn=None):
    from .forecast import projected_events

    if start > end:
        return []
    events = projected_events(database, start, end, conn=conn)
    result = []
    for event in events:
        if event["kind"] != "expense" or event["amount"] >= 0:
            continue
        # Bank cash is the full shared payment. Family spending is the payer’s personal share.
        amount = event.get("personal_amount") if event.get("shared") else -event["amount"]
        if amount is not None:
            owner = from_member(event, members) or account_owners.get(event["account_id"])
            result.append(dict(event, amount=int(amount), member_id=owner))
    return result


def limit_report(database, workspace, month, today, *, only_member=None):
    group_id = active_workspace(workspace)["id"]
    members = members_by_id(workspace)
    start = date.fromisoformat(month_value(month) + "-01")
    end = month_end(start)
    with database.connection(snapshot=True) as conn:
        ledger = database.ledger(conn=conn)
        categories = expense_categories(ledger)
        posted = expense_rows(ledger, members, start, min(today, end))
        owners = {account["id"]: from_member(account, members) for account in ledger.accounts}
        events = future_expenses(database, members, max(today, start), end, owners, conn=conn)
        rules = list(conn.execute("""
            SELECT id, name, member_id, category_id, monthly_limit, starts_month, ends_month
            FROM family_limits WHERE group_id = ? AND starts_month <= ? AND (ends_month IS NULL OR ends_month >= ?)
            ORDER BY name COLLATE NOCASE, id
        """, (group_id, month, month)))
        limits = [
            report_limit(dict(rule), posted, categories, members, events)
            for rule in rules if only_member is None or rule["member_id"] in (None, only_member)
        ]
        if only_member is not None:
            for limit in limits:
                limit["entries"] = [entry for entry in limit["entries"] if entry["member_id"] == only_member]
                limit["own_spent"] = next((row["amount"] for row in limit["members"] if row["id"] == only_member), "0")
                limit["members"] = []
        return {
            "group_id": group_id,
            "month": month,
            "today": today.isoformat(),
            "group_name": workspace.get("name", "Family"),
            "members": workspace["members"],
            "categories": list(categories.values()),
            "limits": limits,
        }


def handle_budgets(handler, path, query, method, today):
    if path != BASE and not path.startswith(BASE + "/"):
        return None
    workspace = getattr(handler, "workspace", None)
    database = getattr(handler, "database", None) or handler.server.database
    match = re.fullmatch(BASE + r"(?:/([1-9][0-9]{0,17}))?", path)
    if not match:
        raise ApiError("This spending limit action was not found.", status=404)
    item_id = int(match[1]) if match[1] else None
    if method == "GET" and item_id is None:
        month = month_value(query.get("month", today.isoformat()[:7]))
        if workspace and workspace.get("kind") == "group":
            return dict(limit_report(database, workspace, month, today), mode="group"), 200
        contexts = getattr(handler, "group_budget_contexts", None)
        groups = [
            limit_report(group_database, group_workspace, month, today, only_member=workspace["id"])
            for group_database, group_workspace in contexts()
        ] if contexts and workspace else []
        return {"mode": "profile", "groups": groups, "month": month, "today": today.isoformat()}, 200
    active_workspace(workspace)
    if (method == "POST" and item_id is None) or (method == "PUT" and item_id is not None):
        saved = write_limit(database, workspace, handler.read_data(8192), item_id)
        return {"id": saved}, 201 if item_id is None else 200
    if method == "DELETE" and item_id is not None:
        last_month = query.get("last_month", today.isoformat()[:7])
        end_limit(database, workspace, item_id, last_month)
        return {"id": item_id}, 200
    raise ApiError("This spending limit action was not found.", status=404)
