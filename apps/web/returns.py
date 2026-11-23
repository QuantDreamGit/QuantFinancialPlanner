"""Recurring interest and coupon credits backed by ordinary ledger commitments."""
import re
from collections import defaultdict
from datetime import date
from itertools import islice

from .forecast import LIQUID, parse_date, rule_dates
from .reports import DAY
from .storage import row_by_id
from .values import ApiError, choice, identifier, line, money
from .workspaces import local_id, owned_account, owner_id


SCHEMA = """
CREATE TABLE IF NOT EXISTS investment_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id INTEGER NOT NULL UNIQUE REFERENCES commitments(id) ON DELETE CASCADE,
    instrument_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    product TEXT NOT NULL CHECK (product IN ('deposit', 'fixed_income')),
    mode TEXT NOT NULL CHECK (mode IN ('fixed', 'rate')),
    principal INTEGER CHECK (principal > 0),
    rate TEXT,
    CHECK ((mode = 'fixed' AND principal IS NULL AND rate IS NULL)
           OR (mode = 'rate' AND principal IS NOT NULL AND rate IS NOT NULL))
);
"""
INSTRUMENTS = {"savings", "investment"}
RATE_SCALE = 1_000_000
MAX_CENTS = 2**63 - 1
MAX_DATE = date(9998, 12, 31)
BASE = "/api/returns"


def initialize_returns(database):
    with database.connection() as conn:
        conn.executescript(SCHEMA)


def monthly_amount(data):
    mode = choice(data.get("mode"), ("fixed", "rate"), "mode")
    if mode == "fixed":
        amount = money(data.get("amount"), "amount")
        if amount <= 0:
            raise ApiError("Enter a monthly return of at least €0.01.", "amount")
        return amount, None, None

    principal = money(data.get("principal"), "principal")
    if principal <= 0:
        raise ApiError("Enter a fixed principal above €0.", "principal")
    value = data.get("rate")
    text = value.strip() if isinstance(value, str) else ""
    match = re.fullmatch(r"[+]?0*([0-9]{1,22})(?:[.,]([0-9]{1,6}))?", text)
    if not match:
        raise ApiError("Enter a positive monthly percentage with up to six decimal places.", "rate")
    whole, fraction = match.groups()
    fraction = (fraction or "").rstrip("0")
    units = int(whole) * RATE_SCALE + int(fraction.ljust(6, "0"))
    denominator = 100 * RATE_SCALE
    amount = (principal * units + denominator // 2) // denominator
    if not 1 <= amount <= MAX_CENTS:
        raise ApiError("The monthly return must round to at least €0.01 and fit within the supported range.", "rate")
    rate = str(int(whole)) + ("." + fraction if fraction else "")
    return amount, principal, rate


def save_return(database, data):
    name = line(data.get("name"), "name", 100)
    amount, principal, rate = monthly_amount(data)
    instrument_id = owned_account(identifier(data.get("instrument_id"), "instrument_id"), "instrument_id")
    payout_id = owned_account(identifier(data.get("payout_account_id"), "payout_account_id"), "payout_account_id")
    category_id = owned_account(identifier(data.get("category_id"), "category_id"), "category_id")
    start = parse_date(data.get("start_on"), "start_on")
    end = parse_date(data["end_on"], "end_on") if data.get("end_on") else None
    if end is not None and end < start:
        raise ApiError("The last date must be on or after the first return.", "end_on")
    with database.connection(write=True) as conn:
        instrument = row_by_id(conn, "accounts", instrument_id, "instrument_id")
        if instrument["kind"] not in INSTRUMENTS:
            raise ApiError("Choose a savings or investment account for the instrument.", "instrument_id")
        default_product = "deposit" if instrument["kind"] == "savings" else "fixed_income"
        product = choice(data.get("product", default_product), ("deposit", "fixed_income"), "product")
        payout_id = instrument_id if payout_id is None else payout_id
        payout = row_by_id(conn, "accounts", payout_id, "payout_account_id")
        if payout_id != instrument_id and payout["kind"] not in LIQUID:
            raise ApiError("Choose a liquid payout account or retain the return in the original instrument.", "payout_account_id")
        if start.isoformat() < max(instrument["opened_on"], payout["opened_on"]):
            raise ApiError("The first return must be on or after both accounts opened.", "start_on")
        if category_id is not None and row_by_id(conn, "categories", category_id, "category_id")["kind"] != "income":
            raise ApiError("Choose an income category for the return.", "category_id")
        commitment_id = conn.execute("""
            INSERT INTO commitments (name, purpose, kind, amount, account_id, destination_id, category_id, start_on, end_on, cadence)
            VALUES (?, 'return', 'income', ?, ?, NULL, ?, ?, ?, 'monthly')
        """, (name, amount, payout_id, category_id, start.isoformat(), end.isoformat() if end else None)).lastrowid
        return conn.execute("""
            INSERT INTO investment_returns (commitment_id, instrument_id, product, mode, principal, rate)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (commitment_id, instrument_id, product, data["mode"], principal, rate)).lastrowid


def end_return(database, return_id, today):
    owned_account(return_id, "return")
    with database.connection(write=True) as conn:
        record = row_by_id(conn, "investment_returns", return_id)
        conn.execute("UPDATE commitments SET archived_on = coalesce(archived_on, ?) WHERE id = ?",
                     (today.isoformat(), record["commitment_id"]))


def return_details(database, today):
    with database.connection(snapshot=True) as conn:
        ledger = database.ledger(conn)
        schedules = [dict(row) for row in conn.execute("""
            SELECT returns.*, c.name, c.amount, c.account_id AS payout_account_id, c.category_id, c.start_on,
                c.end_on, c.archived_on, c.cadence
            FROM investment_returns AS returns JOIN commitments AS c ON c.id = returns.commitment_id
            ORDER BY c.archived_on IS NOT NULL, c.name COLLATE NOCASE, returns.id
        """)]
        resolved = defaultdict(set)
        posted = defaultdict(list)
        skipped = defaultdict(int)
        for item in conn.execute("""
            SELECT o.rule_id, o.due_on, o.entry_id, e.posted_on, e.amount
            FROM commitment_occurrences AS o JOIN investment_returns AS r ON r.commitment_id = o.rule_id
            LEFT JOIN entries AS e ON e.id = o.entry_id ORDER BY o.due_on DESC
        """):
            key = item["rule_id"]
            resolved[key].add(item["due_on"])
            if item["entry_id"] is None:
                skipped[key] += 1
            else:
                posted[key].append({"due_on": item["due_on"], "posted_on": item["posted_on"],
                                    "entry_id": item["entry_id"], "amount": str(item["amount"])})
    accounts = {account["id"]: account for account in ledger.accounts}
    rows = []
    for schedule in schedules:
        key = schedule["commitment_id"]
        completed = resolved[key]

        def pending(start, end):
            return (day.isoformat() for day in rule_dates(schedule, start, end) if day.isoformat() not in completed)

        due = list(islice(pending(date.fromisoformat(schedule["start_on"]), today), 12))
        profile = owner_id(schedule["id"])
        instrument = accounts.get(schedule["instrument_id"], {})
        payout = accounts.get(schedule["payout_account_id"], {})
        recent = posted[key]
        rows.append(dict(schedule,
                         amount=str(schedule["amount"]),
                         principal=str(schedule["principal"]) if schedule["principal"] is not None else None,
                         due=due, due_on=due[0] if due else None,
                         next_on=next(pending(today + DAY, MAX_DATE), None) if today < MAX_DATE else None,
                         posted_count=len(recent), skipped_count=skipped[key], recent=recent[:6],
                         instrument_name=instrument.get("name"), payout_account_name=payout.get("name"),
                         profile_id=profile, profile_name=instrument.get("profile_name"),
                         instrument_source_id=local_id(schedule["instrument_id"]) if profile else schedule["instrument_id"],
                         source_id=local_id(schedule["id"]) if profile else schedule["id"]))
    return {
        "today": today.isoformat(),
        "accounts": [{field: row[field] for field in ("id", "name", "kind", "opened_on", "profile_id", "profile_name")
                      if field in row} for row in ledger.accounts],
        "categories": [row for row in ledger.categories if row["kind"] == "income"],
        "returns": rows,
    }


def handle_returns(handler, path, query, method, today):
    if path != BASE and not path.startswith(BASE + "/"):
        return None
    database = getattr(handler, "database", None) or handler.server.database
    match = re.fullmatch(BASE + r"(?:/(-?[1-9][0-9]{0,17}))?", path)
    if not match:
        raise ApiError("This return action was not found.", status=404)
    if match[1] is None:
        if method == "GET":
            return return_details(database, today), 200
        if method == "POST":
            return {"id": save_return(database, handler.read_data(8192))}, 201
    elif method == "DELETE":
        return_id = int(match[1])
        end_return(database, return_id, today)
        return {"id": return_id}, 200
    raise ApiError("This return action is unavailable.", status=405)
