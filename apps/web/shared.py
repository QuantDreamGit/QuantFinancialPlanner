"""Shared obligations and their cash movements, kept apart from ordinary ledger entries."""

import json
import re
from calendar import monthrange
from collections import defaultdict
from contextlib import nullcontext
from datetime import date, timedelta
from itertools import islice

from .storage import row_by_id, string_labels
from .values import ApiError, choice, identifier, line, money, recorded_date
from .workspaces import owned_account


FREQUENCIES = ("once", "weekly", "monthly", "yearly")
SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS shared_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tag TEXT NOT NULL COLLATE NOCASE UNIQUE
);
CREATE TABLE IF NOT EXISTS shared_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    first_on TEXT NOT NULL,
    end_on TEXT,
    frequency TEXT NOT NULL CHECK (frequency IN ('once', 'weekly', 'monthly', 'yearly')),
    amount INTEGER NOT NULL CHECK (amount > 0),
    account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    payer_id INTEGER REFERENCES shared_people(id) ON DELETE RESTRICT,
    category_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    shares TEXT NOT NULL,
    equal_split INTEGER NOT NULL,
    labels TEXT NOT NULL,
    note TEXT NOT NULL,
    request_key TEXT UNIQUE,
    stopped_at TEXT,
    CHECK ((account_id IS NULL) <> (payer_id IS NULL))
);
CREATE TABLE IF NOT EXISTS shared_postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL REFERENCES shared_rules(id) ON DELETE RESTRICT,
    due_on TEXT NOT NULL,
    posted_on TEXT NOT NULL,
    name TEXT NOT NULL,
    account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    payer_id INTEGER REFERENCES shared_people(id) ON DELETE RESTRICT,
    category_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    gross INTEGER NOT NULL CHECK (gross > 0),
    personal INTEGER NOT NULL CHECK (personal >= 0),
    labels TEXT NOT NULL,
    note TEXT NOT NULL,
    adjustments TEXT NOT NULL,
    voided_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS shared_posting_active
    ON shared_postings (rule_id, due_on) WHERE voided_at IS NULL;
CREATE INDEX IF NOT EXISTS shared_posting_account ON shared_postings (account_id, posted_on);
CREATE TABLE IF NOT EXISTS shared_posting_shares (
    posting_id INTEGER NOT NULL REFERENCES shared_postings(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL,
    member TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    PRIMARY KEY (posting_id, position)
);
CREATE TABLE IF NOT EXISTS shared_settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES shared_people(id) ON DELETE RESTRICT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    posted_on TEXT NOT NULL,
    signed_amount INTEGER NOT NULL CHECK (signed_amount <> 0),
    tag TEXT NOT NULL,
    note TEXT NOT NULL,
    request_key TEXT UNIQUE,
    voided_at TEXT
);
CREATE INDEX IF NOT EXISTS shared_settlement_account ON shared_settlements (account_id, posted_on);
CREATE TABLE IF NOT EXISTS shared_expected (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES shared_people(id) ON DELETE RESTRICT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    due_on TEXT NOT NULL,
    signed_amount INTEGER NOT NULL CHECK (signed_amount <> 0),
    note TEXT NOT NULL,
    tag TEXT NOT NULL,
    request_key TEXT UNIQUE,
    settlement_id INTEGER REFERENCES shared_settlements(id) ON DELETE RESTRICT,
    canceled_at TEXT
);
CREATE TABLE IF NOT EXISTS shared_skips (
    rule_id INTEGER NOT NULL REFERENCES shared_rules(id) ON DELETE RESTRICT,
    due_on TEXT NOT NULL,
    PRIMARY KEY (rule_id, due_on)
);
COMMIT;
"""


def initialize_shared(conn):
    conn.executescript(SCHEMA)


def earliest_account_entry(conn, account_id):
    """Keep account dates consistent with booked and scheduled outgoing payments."""
    return conn.execute("""
        SELECT min(day) FROM (
            SELECT posted_on AS day FROM shared_postings WHERE account_id = ? AND voided_at IS NULL
            UNION ALL SELECT posted_on FROM shared_settlements WHERE account_id = ? AND voided_at IS NULL
            UNION ALL SELECT first_on FROM shared_rules WHERE account_id = ? AND stopped_at IS NULL
            UNION ALL SELECT due_on FROM shared_expected WHERE account_id = ? AND canceled_at IS NULL AND settlement_id IS NULL
        )
    """, (account_id, account_id, account_id, account_id)).fetchone()[0]


def calendar_day(value, field):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    except (TypeError, ValueError):
        pass
    raise ApiError("Enter a valid date.", field)


def payment_dates(rule, start, end):
    anchor = date.fromisoformat(rule["first_on"])
    start = max(start, anchor)
    if rule["end_on"]:
        end = min(end, date.fromisoformat(rule["end_on"]))
    if start > end:
        return
    frequency = rule["frequency"]
    if frequency == "once":
        if start == anchor:
            yield anchor
    elif frequency == "weekly":
        first_offset = ((start - anchor).days + 6) // 7 * 7
        for offset in range(first_offset, (end - anchor).days + 1, 7):
            yield anchor + timedelta(days=offset)
    else:
        step = 1 if frequency == "monthly" else 12
        first_month = anchor.year * 12 + anchor.month - 1
        start_month = start.year * 12 + start.month - 1
        last_month = end.year * 12 + end.month - 1
        first_offset = ((start_month - first_month + step - 1) // step) * step
        for offset in range(first_offset, last_month - first_month + 1, step):
            year, zero_month = divmod(first_month + offset, 12)
            month = zero_month + 1
            day = date(year, month, min(anchor.day, monthrange(year, month)[1]))
            if start <= day <= end:
                yield day


def _ignored_dates(conn):
    by_rule = defaultdict(set)
    for row in conn.execute("""
        SELECT rule_id, due_on FROM shared_postings WHERE voided_at IS NULL
        UNION SELECT rule_id, due_on FROM shared_skips
    """):
        by_rule[row["rule_id"]].add(row["due_on"])
    return by_rule


def _pending_dates(rule, start, end, ignored):
    return (day for day in payment_dates(rule, start, end) if day.isoformat() not in ignored)


def projected_cash(database, start, end, *, conn=None):
    """Return unposted bank movements scheduled in the inclusive period."""
    result = []
    with (nullcontext(conn) if conn is not None else database.connection(snapshot=True)) as conn:
        ignored = _ignored_dates(conn)
        for rule in conn.execute("SELECT * FROM shared_rules WHERE stopped_at IS NULL AND account_id IS NOT NULL"):
            personal = json.loads(rule["shares"])[0]["amount"]
            for day in _pending_dates(rule, start, end, ignored[rule["id"]]):
                result.append({
                    "date": day, "amount": -rule["amount"], "account_id": rule["account_id"],
                    "name": rule["name"], "kind": "expense", "category_id": rule["category_id"],
                    "shared": True, "rule_id": rule["id"], "personal_amount": str(personal),
                    "owed_back": str(rule["amount"] - personal),
                })
        for plan, funded in _expected_status(conn):
            day = date.fromisoformat(plan["due_on"])
            if funded and start <= day <= end:
                direction = "Reimbursement from " if plan["signed_amount"] > 0 else "Repayment to "
                result.append({
                    "date": day, "amount": plan["signed_amount"], "account_id": plan["account_id"],
                    "name": direction + "@" + plan["tag"], "kind": "settlement", "category_id": None,
                    "shared": True, "scheduled_settlement_id": plan["id"],
                })
        if getattr(database, "kind", None) == "group":
            result.extend(database.member_shared_cash(start, end, conn))
    return sorted(result, key=lambda item: item["date"])


def recurring_cash_rules(database, *, conn=None):
    """List the shared recurring expenses eligible for what-if changes."""
    with (nullcontext(conn) if conn is not None else database.connection()) as connection:
        rules = [dict(row) for row in connection.execute("""
            SELECT id, name, frequency FROM shared_rules
            WHERE account_id IS NOT NULL AND stopped_at IS NULL AND frequency <> 'once'
            ORDER BY name COLLATE NOCASE, id
        """)]
        if getattr(database, "kind", None) == "group":
            rules.extend(database.member_shared_rules(connection))
        return rules


def _save_person(conn, data, person_id=None):
    name = line(data.get("name"), "name", 60)
    tag = line(data.get("tag"), "tag", 32).removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,30}", tag):
        raise ApiError("Use 1–31 letters, numbers, dots, dashes or underscores.", "tag")
    if conn.execute("SELECT 1 FROM shared_people WHERE tag = ? AND id <> ?", (tag, person_id or 0)).fetchone():
        raise ApiError("That tag already belongs to someone.", "tag", status=409)
    if person_id is None:
        return conn.execute("INSERT INTO shared_people (name, tag) VALUES (?, ?)", (name, tag)).lastrowid
    row_by_id(conn, "shared_people", person_id)
    conn.execute("UPDATE shared_people SET name = ?, tag = ? WHERE id = ?", (name, tag, person_id))
    return person_id


def _cash_account(conn, account_id, posted_on):
    owned_account(account_id)
    account = row_by_id(conn, "accounts", account_id, "account_id")
    if account["kind"] == "investment":
        raise ApiError("Choose a checking, savings or cash account.", "account_id")
    if posted_on < account["opened_on"]:
        raise ApiError("Choose a date on or after your account opening date.", "posted_on")
    return account


def _shares(conn, data, amount):
    participants = data.get("participants")
    if not isinstance(participants, list) or not 1 <= len(participants) <= 24:
        raise ApiError("Select between 1 and 24 other people.", "participants")
    people = [identifier(participant, "participants") for participant in participants]
    if len(set(people)) != len(people) or None in people:
        raise ApiError("Select each person once.", "participants")
    for person in people:
        row_by_id(conn, "shared_people", person, "participants")
    split = choice(data.get("split", "equal"), ("equal", "custom"), "split")
    members = [None, *sorted(people)]
    if split == "equal":
        base, remainder = divmod(amount, len(members))
        portions = [base + (idx < remainder) for idx in range(len(members))]
    else:
        values = data.get("shares")
        keys = ["me" if person is None else str(person) for person in members]
        if not isinstance(values, dict) or set(values) != set(keys):
            raise ApiError("Enter a share for every participant.", "shares")
        portions = [money(values[key], "shares") for key in keys]
        if any(cents < 0 for cents in portions) or sum(portions) != amount:
            raise ApiError("Use nonnegative shares that add up to the amount paid.", "shares")
    return [{"person_id": person, "amount": cents} for person, cents in zip(members, portions)], split == "equal"


def _rule_fields(conn, data):
    name = line(data.get("name"), "name", 100)
    first = calendar_day(data.get("first_on"), "first_on")
    frequency = choice(data.get("frequency", "once"), FREQUENCIES, "frequency")
    end = calendar_day(data["end_on"], "end_on") if data.get("end_on") else None
    if end and (end < first or frequency == "once"):
        raise ApiError("Choose a last day on or after the first recurring payment.", "end_on")
    amount = money(data.get("amount"), "amount")
    if amount <= 0:
        raise ApiError("Enter an amount above €0.", "amount")
    payer = identifier(data.get("payer_id"), "payer_id")
    account_id = identifier(data.get("account_id"), "account_id")
    if payer is None:
        _cash_account(conn, account_id, first.isoformat())
    else:
        row_by_id(conn, "shared_people", payer, "payer_id")
        account_id = None
    category_id = identifier(data.get("category_id"), "category_id")
    if category_id is not None and row_by_id(conn, "categories", category_id, "category_id")["kind"] != "expense":
        raise ApiError("Choose an expense category.", "category_id")
    shares, equal_split = _shares(conn, data, amount)
    labels = string_labels(data.get("labels", []))
    note = line(data.get("note", ""), "note", 600, required=False)
    return (name, first.isoformat(), end.isoformat() if end else None, frequency, amount, account_id,
            payer, category_id, json.dumps(shares), equal_split, json.dumps(labels), note)


def _save_rule(conn, data, today, rule_id=None):
    key = line(data.get("request_key", ""), "request_key", 64, required=False) or None
    previous = conn.execute("SELECT * FROM shared_rules WHERE request_key = ?", (key,)).fetchone() if key and rule_id is None else None
    columns = ("name", "first_on", "end_on", "frequency", "amount", "account_id", "payer_id",
               "category_id", "shares", "equal_split", "labels", "note")
    values = _rule_fields(conn, data)
    if previous:
        if tuple(previous[col] for col in columns) != values:
            raise ApiError("This form already saved a payment. Reopen the shared payment to edit it.", status=409)
        return previous["id"]
    if rule_id is not None:
        old = _active_rule(conn, rule_id)
        if (values[1], values[3]) != (old["first_on"], old["frequency"]) and conn.execute("""
            SELECT 1 FROM shared_postings WHERE rule_id = ? UNION SELECT 1 FROM shared_skips WHERE rule_id = ?
        """, (rule_id, rule_id)).fetchone():
            raise ApiError("End this schedule and create a new one to change its starting date or frequency.", "first_on")
        conn.execute("""
            UPDATE shared_rules SET name = ?, first_on = ?, end_on = ?, frequency = ?, amount = ?, account_id = ?,
                payer_id = ?, category_id = ?, shares = ?, equal_split = ?, labels = ?, note = ? WHERE id = ?
        """, (*values, rule_id))
        return rule_id
    if type(data.get("record_now", False)) is not bool:
        raise ApiError("Choose whether to record the first payment now.", "record_now")
    rule_id = conn.execute("""
        INSERT INTO shared_rules (name, first_on, end_on, frequency, amount, account_id, payer_id,
            category_id, shares, equal_split, labels, note, request_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (*values, key)).lastrowid
    if data.get("record_now"):
        _post_rule(conn, rule_id, {"due_on": values[1], "posted_on": values[1]}, today)
    return rule_id


def _active_rule(conn, rule_id):
    rule = row_by_id(conn, "shared_rules", rule_id)
    if rule["stopped_at"] is not None:
        raise ApiError("That schedule has ended.", status=409)
    return rule


def _due_date(rule, data):
    due = calendar_day(data.get("due_on"), "due_on")
    if not any(payment_dates(rule, due, due)):
        raise ApiError("Choose a date on this payment schedule.", "due_on")
    return due.isoformat()


def _post_rule(conn, rule_id, data, today):
    rule = _active_rule(conn, rule_id)
    due = _due_date(rule, data)
    posted = recorded_date(data.get("posted_on"), "posted_on", today)
    already = conn.execute("""
        SELECT id, posted_on FROM shared_postings WHERE rule_id = ? AND due_on = ? AND voided_at IS NULL
    """, (rule_id, due)).fetchone()
    if already:
        if already["posted_on"] != posted:
            raise ApiError("That occurrence was recorded with a different payment date.", status=409)
        return already["id"]
    if conn.execute("SELECT 1 FROM shared_skips WHERE rule_id = ? AND due_on = ?", (rule_id, due)).fetchone():
        raise ApiError("That occurrence was skipped.", status=409)
    if rule["account_id"] is not None:
        _cash_account(conn, rule["account_id"], posted)
    shares = json.loads(rule["shares"])
    personal = shares[0]["amount"]
    people = {row["id"]: row for row in conn.execute("SELECT id, tag, name FROM shared_people")}
    participants = [people[item["person_id"]] for item in shares[1:]]
    payer = people.get(rule["payer_id"])
    if payer is None:
        adjustments = {str(item["person_id"]): item["amount"] for item in shares[1:] if item["amount"]}
    else:
        adjustments = {str(payer["id"]): -personal} if personal else {}
    # Person tags are system-provided and remain searchable after later renames.
    labels = list(dict.fromkeys([*json.loads(rule["labels"]),
                *["@" + item["tag"] for item in participants], *(["@" + payer["tag"]] if payer else [])]))
    paid_by = f'Paid by {payer["name"]}.' if payer else "Paid by you."
    note = " ".join(filter(None, (paid_by, rule["note"])))
    posting_id = conn.execute("""
        INSERT INTO shared_postings (rule_id, due_on, posted_on, name, account_id, payer_id, category_id,
            gross, personal, labels, note, adjustments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (rule_id, due, posted, rule["name"], rule["account_id"], rule["payer_id"], rule["category_id"],
          rule["amount"], personal, json.dumps(labels), note, json.dumps(adjustments))).lastrowid
    members = ["You", *["@" + person["tag"] for person in participants]]
    conn.executemany("""
        INSERT INTO shared_posting_shares (posting_id, position, member, amount) VALUES (?, ?, ?, ?)
    """, ((posting_id, position, label, item["amount"]) for position, (label, item) in enumerate(zip(members, shares))))
    return posting_id


def _skip_rule(conn, rule_id, data):
    due = _due_date(_active_rule(conn, rule_id), data)
    if conn.execute("""
        SELECT 1 FROM shared_postings WHERE rule_id = ? AND due_on = ? AND voided_at IS NULL
    """, (rule_id, due)).fetchone():
        raise ApiError("Void the recorded payment before skipping its occurrence.", status=409)
    conn.execute("INSERT OR IGNORE INTO shared_skips (rule_id, due_on) VALUES (?, ?)", (rule_id, due))
    return rule_id


def _balances(conn, through=None):
    result = defaultdict(int)
    for row in conn.execute("""
        SELECT adjustments FROM shared_postings WHERE voided_at IS NULL AND (? IS NULL OR posted_on <= ?)
    """, (through, through)):
        for person, amount in json.loads(row["adjustments"]).items():
            result[int(person)] += amount
    for row in conn.execute("""
        SELECT person_id, signed_amount FROM shared_settlements
        WHERE voided_at IS NULL AND (? IS NULL OR posted_on <= ?)
    """, (through, through)):
        result[row["person_id"]] -= row["signed_amount"]
    return result


def _settle(conn, data, today):
    person_id = identifier(data.get("person_id"), "person_id")
    person = row_by_id(conn, "shared_people", person_id, "person_id")
    account_id = identifier(data.get("account_id"), "account_id")
    posted = recorded_date(data.get("posted_on"), "posted_on", today)
    _cash_account(conn, account_id, posted)
    amount = money(data.get("amount"), "amount")
    direction = choice(data.get("direction"), ("receive", "pay"), "direction")
    sign = 1 if direction == "receive" else -1
    if amount <= 0:
        raise ApiError("Enter an amount above €0.", "amount")
    note = line(data.get("note", ""), "note", 600, required=False)
    key = line(data.get("request_key", ""), "request_key", 64, required=False) or None
    previous = conn.execute("SELECT * FROM shared_settlements WHERE request_key = ?", (key,)).fetchone() if key else None
    values = (person_id, account_id, posted, amount * sign, note)
    if previous:
        if tuple(previous[col] for col in ("person_id", "account_id", "posted_on", "signed_amount", "note")) != values:
            raise ApiError("This form already recorded a settlement.", status=409)
        return previous["id"]
    on_date = _balances(conn, posted)[person_id] * sign
    current = _balances(conn)[person_id] * sign
    if amount > min(current, on_date):
        raise ApiError("The settlement exceeds the outstanding balance in that direction.", "amount")
    return conn.execute("""
        INSERT INTO shared_settlements (person_id, account_id, posted_on, signed_amount, note, tag, request_key)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (*values, person["tag"], key)).lastrowid


def _expected_status(conn, balances=None):
    """Reserve outstanding balances for scheduled settlements in date order."""
    remaining = (balances if balances is not None else _balances(conn)).copy()
    for row in conn.execute("""
        SELECT * FROM shared_expected WHERE canceled_at IS NULL AND settlement_id IS NULL ORDER BY due_on, id
    """):
        amount = row["signed_amount"]
        person = row["person_id"]
        available = remaining.get(person, 0)
        sign = 1 if amount > 0 else -1
        funded = abs(amount) <= sign * available
        if funded:
            remaining[person] -= amount
        yield row, funded


def _schedule_settlement(conn, data, today):
    person_id = identifier(data.get("person_id"), "person_id")
    person = row_by_id(conn, "shared_people", person_id, "person_id")
    account = identifier(data.get("account_id"), "account_id")
    day = calendar_day(data.get("due_on"), "due_on")
    _cash_account(conn, account, day.isoformat())
    amount = money(data.get("amount"), "amount")
    direction = choice(data.get("direction"), ("receive", "pay"), "direction")
    sign = 1 if direction == "receive" else -1
    if amount <= 0:
        raise ApiError("Enter an amount above €0.", "amount")
    note = line(data.get("note", ""), "note", 600, required=False)
    key = line(data.get("request_key", ""), "request_key", 64, required=False) or None
    values = (person_id, account, day.isoformat(), amount * sign, note)
    previous = conn.execute("SELECT * FROM shared_expected WHERE request_key = ?", (key,)).fetchone() if key else None
    if previous:
        if tuple(previous[col] for col in ("person_id", "account_id", "due_on", "signed_amount", "note")) != values:
            raise ApiError("This form already scheduled a settlement.", status=409)
        return previous["id"]
    if day < today:
        raise ApiError("Choose today or a future date for a scheduled settlement.", "due_on")
    balances = _balances(conn)
    reserved = sum(plan["signed_amount"] for plan, funded in _expected_status(conn, balances)
                   if funded and plan["person_id"] == person_id)
    if amount > sign * (balances[person_id] - reserved):
        raise ApiError("The amount exceeds the balance available to schedule.", "amount")
    return conn.execute("""
        INSERT INTO shared_expected (person_id, account_id, due_on, signed_amount, note, tag, request_key)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (*values, person["tag"], key)).lastrowid


def _record_expected(conn, item_id, data, today):
    plan = row_by_id(conn, "shared_expected", item_id)
    if plan["canceled_at"]:
        raise ApiError("That settlement was canceled.", status=409)
    if plan["settlement_id"]:
        return plan["settlement_id"]
    cents = abs(plan["signed_amount"])
    settlement = _settle(conn, {
        "person_id": plan["person_id"],
        "account_id": plan["account_id"],
        "posted_on": data.get("posted_on"),
        "amount": f"{cents // 100}.{cents % 100:02}",
        "direction": "receive" if plan["signed_amount"] > 0 else "pay",
        "note": plan["note"],
    }, today)
    conn.execute("UPDATE shared_expected SET settlement_id = ? WHERE id = ?", (settlement, item_id))
    return settlement


def _void(conn, table, item_id):
    row_by_id(conn, table, item_id)
    conn.execute(f"UPDATE {table} SET voided_at = CURRENT_TIMESTAMP WHERE id = ? AND voided_at IS NULL", (item_id,))
    return item_id


def ledger_entries(conn):
    entries = []
    for row in conn.execute("SELECT * FROM shared_postings WHERE voided_at IS NULL ORDER BY id"):
        cash_delta = -row["gross"] if row["account_id"] is not None else 0
        shared_delta = sum(json.loads(row["adjustments"]).values())
        entries.append({
            "id": -2 * row["id"], "kind": "expense", "name": row["name"], "posted_on": row["posted_on"],
            "account_id": row["account_id"], "destination_id": None, "category_id": row["category_id"],
            "amount": row["personal"], "labels": json.loads(row["labels"]), "note": row["note"],
            "cash_delta": cash_delta, "shared_delta": shared_delta,
            "shared": {"type": "payment", "id": row["id"], "rule_id": row["rule_id"], "gross": str(row["gross"])},
        })
    for row in conn.execute("SELECT * FROM shared_settlements WHERE voided_at IS NULL ORDER BY id"):
        amount = row["signed_amount"]
        tag = "@" + row["tag"]
        direction = "Reimbursement from " if amount > 0 else "Reimbursement to "
        entries.append({
            "id": -2 * row["id"] - 1, "kind": "settlement", "name": direction + tag, "posted_on": row["posted_on"],
            "account_id": row["account_id"], "destination_id": None, "category_id": None,
            "amount": abs(amount), "labels": [tag], "note": row["note"], "cash_delta": amount,
            "shared_delta": -amount, "shared": {"type": "settlement", "id": row["id"]},
        })
    return entries


def _view(conn, today):
    balances = _balances(conn)
    people = [dict(row, balance=str(balances[row["id"]]))
              for row in conn.execute("SELECT * FROM shared_people ORDER BY name COLLATE NOCASE, id")]
    ignored = _ignored_dates(conn)
    rules = []
    future_end = today + timedelta(days=min(400, (date.max - today).days))
    for row in conn.execute("SELECT * FROM shared_rules ORDER BY stopped_at IS NOT NULL, id DESC"):
        pending = [] if row["stopped_at"] else [
            day.isoformat() for day in islice(_pending_dates(row, date.min, future_end, ignored[row["id"]]), 8)
        ]
        if not pending and not row["stopped_at"] and row["first_on"] > future_end.isoformat():
            pending = [row["first_on"]]
        rules.append({
            **{key: row[key] for key in ("id", "name", "frequency", "first_on", "end_on", "account_id",
               "payer_id", "category_id", "note", "stopped_at")},
            "amount": str(row["amount"]), "labels": json.loads(row["labels"]), "split": "equal" if row["equal_split"] else "custom",
            "shares": [dict(part, amount=str(part["amount"])) for part in json.loads(row["shares"])],
            "pending": pending,
        })
    history = []
    posting_shares = defaultdict(list)
    for part in conn.execute("SELECT posting_id, member, amount FROM shared_posting_shares ORDER BY posting_id, position"):
        posting_shares[part["posting_id"]].append({"member": part["member"], "amount": str(part["amount"])})
    for row in conn.execute("""
        SELECT id, name, posted_on, due_on, rule_id, gross, personal, account_id, payer_id, labels, voided_at
        FROM shared_postings ORDER BY posted_on DESC, id DESC
    """):
        history.append(dict(row, type="payment", gross=str(row["gross"]), personal=str(row["personal"]),
                            labels=json.loads(row["labels"]), shares=posting_shares[row["id"]]))
    for row in conn.execute("""
        SELECT id, person_id, posted_on, account_id, signed_amount, tag, voided_at
        FROM shared_settlements ORDER BY posted_on DESC, id DESC
    """):
        history.append(dict(row, type="settlement", signed_amount=str(row["signed_amount"])))
    return {
        "today": today.isoformat(), "people": people, "rules": rules,
        "history": sorted(history, key=lambda item: (item["posted_on"], item["id"]), reverse=True),
        "accounts": [dict(row) for row in conn.execute(
            "SELECT id, name, opened_on FROM accounts WHERE id > 0 AND kind <> 'investment' ORDER BY name COLLATE NOCASE"
        )],
        "categories": [dict(row) for row in conn.execute(
            "SELECT id, name, parent_id FROM categories WHERE id > 0 AND kind = 'expense' ORDER BY name COLLATE NOCASE"
        )],
        "owed": str(sum(value for value in balances.values() if value > 0)),
        "owe": str(-sum(value for value in balances.values() if value < 0)),
        "expected": [
            {**{key: row[key] for key in ("id", "person_id", "account_id", "due_on", "tag")},
             "signed_amount": str(row["signed_amount"]), "funded": funded}
            for row, funded in _expected_status(conn, balances)
        ],
    }


def handle_shared(handler, path, query, method, today):
    if path != "/api/shared" and not path.startswith("/api/shared/"):
        return None
    db = getattr(handler, "database", handler.server.database)
    if path == "/api/shared" and method == "GET":
        with db.connection(snapshot=True) as conn:
            view = _view(conn, today)
        if getattr(db, "kind", None) == "group":
            members = []
            for member in db.registry.workspace(db.ident)["members"]:
                source = db.registry.database(dict(member, kind="profile"))
                with source.connection(snapshot=True) as conn:
                    amounts = _balances(conn)
                    pending = list(_expected_status(conn, amounts))
                members.append(dict(
                    member,
                    owed=str(sum(value for value in amounts.values() if value > 0)),
                    owe=str(-sum(value for value in amounts.values() if value < 0)),
                    overdue=sum(item["signed_amount"] > 0 and item["due_on"] < today.isoformat() for item, _ in pending),
                ))
            view["members"] = members
        return view, 200
    route = re.fullmatch(
        r"/api/shared/(people|rules|postings|settlements|expected)(?:/([1-9][0-9]{0,17})(?:/(record|skip|stop|void|cancel))?)?", path,
    )
    if not route:
        raise ApiError("This shared payment action was not found.", status=404)
    resource, raw_id, action = route.groups()
    item_id = int(raw_id) if raw_id else None
    if method not in ("POST", "PUT"):
        raise ApiError("This method is unavailable for shared payments.", status=405)
    data = handler.read_data(16_384)
    with db.connection(write=True) as conn:
        if resource == "people" and action is None and (method == "PUT") == bool(item_id):
            key = _save_person(conn, data, item_id)
        elif resource == "rules" and action is None and (method == "PUT") == bool(item_id):
            key = _save_rule(conn, data, today, item_id)
        elif resource == "rules" and method == "POST" and action == "record":
            key = _post_rule(conn, item_id, data, today)
        elif resource == "rules" and method == "POST" and action == "skip":
            key = _skip_rule(conn, item_id, data)
        elif resource == "rules" and method == "POST" and action == "stop":
            row_by_id(conn, "shared_rules", item_id)
            conn.execute("UPDATE shared_rules SET stopped_at = CURRENT_TIMESTAMP WHERE id = ? AND stopped_at IS NULL", (item_id,))
            key = item_id
        elif resource == "settlements" and method == "POST" and item_id is None:
            key = _settle(conn, data, today)
        elif resource == "expected" and method == "POST" and item_id is None:
            key = _schedule_settlement(conn, data, today)
        elif resource == "expected" and method == "POST" and action == "record":
            key = _record_expected(conn, item_id, data, today)
        elif resource == "expected" and method == "POST" and action == "cancel":
            row_by_id(conn, "shared_expected", item_id)
            conn.execute("UPDATE shared_expected SET canceled_at = CURRENT_TIMESTAMP WHERE id = ? AND settlement_id IS NULL", (item_id,))
            key = item_id
        elif resource in ("postings", "settlements") and method == "POST" and action == "void":
            key = _void(conn, "shared_" + resource, item_id)
        else:
            raise ApiError("This shared payment action is unavailable.", status=405)
    return {"id": key}, (201 if method == "POST" and item_id is None else 200)
