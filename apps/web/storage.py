import json
import os
import sqlite3
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import date

from .values import ApiError, choice, identifier, line, money, recorded_date


ACCOUNT_KINDS = ("checking", "savings", "investment", "cash")
FLOW_KINDS = ("income", "expense", "investment")
ENTRY_KINDS = (*FLOW_KINDS, "transfer", "valuation")
COLORS = ("indigo", "mint", "rose", "sky", "amber", "violet", "slate")

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('checking', 'savings', 'investment', 'cash')),
    opened_on TEXT NOT NULL,
    opening_balance INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE,
    kind TEXT NOT NULL CHECK (kind IN ('income', 'expense', 'investment')),
    parent_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    color TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS category_name
    ON categories(kind, coalesce(parent_id, 0), name);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('income', 'expense', 'investment', 'transfer', 'valuation')),
    name TEXT NOT NULL,
    posted_on TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (
        (kind = 'valuation' AND amount <> 0) OR (kind <> 'valuation' AND amount > 0)
    ),
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    destination_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    category_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    labels TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    request_key TEXT UNIQUE,
    statement_expected INTEGER CHECK (statement_expected IN (0, 1)),
    CHECK (
        (kind IN ('investment', 'transfer') AND destination_id IS NOT NULL AND account_id <> destination_id)
        OR (kind NOT IN ('investment', 'transfer') AND destination_id IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS entries_posted ON entries(posted_on, id);
CREATE INDEX IF NOT EXISTS entries_accounts ON entries(account_id, posted_on);
CREATE INDEX IF NOT EXISTS entries_destinations ON entries(destination_id, posted_on);
CREATE INDEX IF NOT EXISTS entries_categories ON entries(category_id);
CREATE TABLE IF NOT EXISTS scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    input TEXT NOT NULL,
    result TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
PRAGMA user_version = 1;
COMMIT;
"""


@dataclass
class Ledger:
    accounts: list[dict]
    categories: list[dict]
    entries: list[dict]


def row_by_id(conn, table, item_id, field=None):
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        if field:
            raise ApiError("Choose an existing item.", field)
        raise ApiError("This item no longer exists.", status=404)
    return row


def string_labels(value):
    if not isinstance(value, list) or len(value) > 12:
        raise ApiError("Use up to 12 labels, separated by commas.", "labels")
    labels = {}
    for item in value:
        label = line(item, "labels", 32)
        labels.setdefault(label.casefold(), label)
    return list(labels.values())


def expects_statement(kind, source_kind, destination_kind=None):
    return kind != "valuation" and (source_kind in ("checking", "savings") or
                                    destination_kind in ("checking", "savings"))


class Database:
    def __init__(self, path):
        self.path = path

    def initialize(self):
        from .attachments import initialize_attachments
        from .budgets import initialize_budgets
        from .cars import initialize_cars
        from .decision import initialize_decision
        from .forecast import initialize_forecast
        from .funding_policy import initialize_funding
        from .goals import initialize_goals
        from .imports import initialize_imports
        from .returns import initialize_returns
        from .shared import initialize_shared
        from .statement_documents import initialize_statement_documents

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            pass
        with self.connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("This database requires a newer version of the dashboard.")
            conn.execute("PRAGMA journal_mode = WAL")
            if version == 0:
                conn.executescript(SCHEMA)
            initialize_shared(conn)
        with self.connection(write=True) as conn:
            if not any(row["name"] == "statement_expected" for row in conn.execute("PRAGMA table_info(entries)")):
                conn.execute("ALTER TABLE entries ADD COLUMN statement_expected INTEGER CHECK (statement_expected IN (0, 1))")
        initialize_forecast(self)
        initialize_returns(self)
        initialize_goals(self)
        initialize_funding(self)
        initialize_decision(self)
        initialize_imports(self)
        initialize_budgets(self)
        initialize_cars(self)
        initialize_attachments(self)
        initialize_statement_documents(self)

    @contextmanager
    def connection(self, *, write=False, snapshot=False):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                elif snapshot:
                    conn.execute("BEGIN")
                yield conn
        finally:
            conn.close()

    def ledger(self, conn=None):
        from .shared import ledger_entries

        with (nullcontext(conn) if conn is not None else self.connection(snapshot=True)) as conn:
            accounts = [dict(row) for row in conn.execute("SELECT * FROM accounts ORDER BY id")]
            account_kinds = {account["id"]: account["kind"] for account in accounts}
            categories = [dict(row) for row in conn.execute("SELECT * FROM categories ORDER BY name, id")]
            entries = []
            for row in conn.execute("""
                SELECT id, kind, name, posted_on, amount, account_id, destination_id,
                       category_id, labels, note, statement_expected,
                       (SELECT count(*) FROM entry_attachments WHERE entry_id = entries.id) AS attachment_count
                FROM entries ORDER BY posted_on, id
            """):
                statement = row["statement_expected"]
                if statement is None:
                    statement = expects_statement(row["kind"], account_kinds[row["account_id"]],
                                                  account_kinds.get(row["destination_id"]))
                entries.append(dict(row, labels=json.loads(row["labels"]), statement_expected=bool(statement)))
            entries.extend(ledger_entries(conn))
            entries.sort(key=lambda entry: (entry["posted_on"], entry["id"]))
        return Ledger(accounts, categories, entries)

    def save_account(self, data, today, item_id=None):
        from .shared import earliest_account_entry

        name = line(data.get("name"), "name", 70)
        opened_on = recorded_date(data.get("opened_on"), "opened_on", today)
        opening_balance = money(data.get("opening_balance"), "opening_balance")
        with self.connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM accounts WHERE name = ? AND id <> ?",
                            (name, item_id or 0)).fetchone():
                raise ApiError("An account with this name already exists.", "name")
            if item_id is None:
                kind = choice(data.get("kind"), ACCOUNT_KINDS, "kind")
                return conn.execute(
                    "INSERT INTO accounts (name, kind, opened_on, opening_balance) VALUES (?, ?, ?, ?)",
                    (name, kind, opened_on, opening_balance),
                ).lastrowid
            row_by_id(conn, "accounts", item_id)
            first_shared = earliest_account_entry(conn, item_id)
            if first_shared and first_shared < opened_on:
                raise ApiError("The opening date must be on or before this account’s earliest shared payment.", "opened_on")
            if conn.execute("""
                SELECT 1 FROM commitments
                WHERE (account_id = ? OR destination_id = ?) AND archived_on IS NULL AND start_on < ?
            """, (item_id, item_id, opened_on)).fetchone():
                raise ApiError("The opening date must be on or before this account’s first scheduled commitment.", "opened_on")
            if conn.execute("""
                SELECT 1 FROM investment_returns AS r JOIN commitments AS c ON c.id = r.commitment_id
                WHERE r.instrument_id = ? AND c.start_on < ? AND c.archived_on IS NULL
            """, (item_id, opened_on)).fetchone():
                raise ApiError("The opening date must be on or before this account’s first scheduled return.", "opened_on")
            if conn.execute("""
                SELECT 1 FROM entries WHERE (account_id = ? OR destination_id = ?) AND posted_on < ?
            """, (item_id, item_id, opened_on)).fetchone():
                raise ApiError("The opening date must be on or before this account’s earliest transaction.", "opened_on")
            conn.execute("UPDATE accounts SET name = ?, opened_on = ?, opening_balance = ? WHERE id = ?",
                         (name, opened_on, opening_balance, item_id))
            return item_id

    def save_category(self, data, item_id=None):
        name = line(data.get("name"), "name", 60)
        color = choice(data.get("color"), COLORS, "color")
        parent_id = identifier(data.get("parent_id"), "parent_id")
        with self.connection(write=True) as conn:
            old = row_by_id(conn, "categories", item_id) if item_id is not None else None
            kind = old["kind"] if old else choice(data.get("kind"), FLOW_KINDS, "kind")
            if parent_id is not None:
                parent = row_by_id(conn, "categories", parent_id, "parent_id")
                if parent["kind"] != kind:
                    raise ApiError("Choose a parent in the same money flow.", "parent_id")
            if old and parent_id is not None:
                parents = dict(conn.execute("SELECT id, parent_id FROM categories WHERE kind = ?", (kind,)))
                ancestor = parent_id
                while ancestor is not None:
                    if ancestor == item_id:
                        raise ApiError("Choose a parent outside this category’s branch.", "parent_id")
                    ancestor = parents[ancestor]
            if conn.execute("""
                SELECT 1 FROM categories WHERE kind = ? AND name = ? AND parent_id IS ? AND id <> ?
            """, (kind, name, parent_id, item_id or 0)).fetchone():
                raise ApiError("A category with this name already exists at this level.", "name")
            if old:
                conn.execute("UPDATE categories SET name = ?, parent_id = ?, color = ? WHERE id = ?",
                             (name, parent_id, color, item_id))
                return item_id
            return conn.execute("INSERT INTO categories (name, kind, parent_id, color) VALUES (?, ?, ?, ?)",
                                (name, kind, parent_id, color)).lastrowid

    def save_entry(self, data, today, item_id=None, *, conn=None):
        name = line(data.get("name"), "name", 100)
        kind = choice(data.get("kind"), ENTRY_KINDS, "kind")
        amount = money(data.get("amount"), "amount")
        if amount == 0 or (kind != "valuation" and amount < 0):
            issue = "Enter a nonzero change in value." if kind == "valuation" else "Enter an amount above €0."
            raise ApiError(issue, "amount")
        posted_on = recorded_date(data.get("posted_on"), "posted_on", today)
        account_id = identifier(data.get("account_id"), "account_id")
        destination_id = identifier(data.get("destination_id"), "destination_id")
        category_id = identifier(data.get("category_id"), "category_id")
        labels = string_labels(data.get("labels", []))
        note = line(data.get("note", ""), "note", 600, required=False)
        request_key = line(data.get("request_key", ""), "request_key", 64, required=False) or None
        statement = data.get("statement_expected")
        if "statement_expected" in data and type(statement) is not bool:
            raise ApiError("Choose whether the transaction appears on a statement.", "statement_expected")

        with (nullcontext(conn) if conn is not None else self.connection(write=True)) as conn:
            previous = None
            if item_id is None and request_key:
                previous = conn.execute("""
                    SELECT id, kind, name, posted_on, amount, account_id, destination_id, category_id,
                           labels, note, statement_expected
                    FROM entries WHERE request_key = ?
                """, (request_key,)).fetchone()
            if item_id is not None:
                current = row_by_id(conn, "entries", item_id)
                if statement is None:
                    statement = current["statement_expected"]
            account = row_by_id(conn, "accounts", account_id, "account_id")
            if posted_on < account["opened_on"]:
                raise ApiError("Choose a date on or after the account opening date.", "posted_on")
            destination = None
            if kind in ("transfer", "investment"):
                destination = row_by_id(conn, "accounts", destination_id, "destination_id")
                if account_id == destination_id:
                    raise ApiError("Choose a different destination account.", "destination_id")
                if posted_on < destination["opened_on"]:
                    raise ApiError("Choose a date on or after both account opening dates.", "posted_on")
                if kind == "investment" and (account["kind"] == "investment" or destination["kind"] != "investment"):
                    raise ApiError("Choose a cash source and an investment destination.", "destination_id")
            elif destination_id is not None:
                raise ApiError("This transaction type uses one account.", "destination_id")
            if kind == "valuation" and account["kind"] != "investment":
                raise ApiError("Choose an investment account.", "account_id")
            if category_id is not None:
                category = row_by_id(conn, "categories", category_id, "category_id")
                if category["kind"] != kind:
                    raise ApiError("Choose a category for this transaction type.", "category_id")
            default_statement = expects_statement(kind, account["kind"], destination["kind"] if destination else None)
            statement = False if kind == "valuation" else (default_statement if statement is None else statement)
            values = (kind, name, posted_on, amount, account_id, destination_id, category_id, json.dumps(labels), note, statement)
            if previous is not None:
                old_values = tuple(previous)[1:-1]
                old_statement = previous["statement_expected"]
                if old_values != values[:-1] or bool(default_statement if old_statement is None else old_statement) != statement:
                    raise ApiError("This form already saved a transaction. Edit it from the ledger.", status=409)
                return previous["id"]
            if item_id is not None:
                conn.execute("""
                    UPDATE entries SET kind = ?, name = ?, posted_on = ?, amount = ?, account_id = ?,
                        destination_id = ?, category_id = ?, labels = ?, note = ?, statement_expected = ? WHERE id = ?
                """, (*values, item_id))
                conn.execute("""
                    DELETE FROM commitment_occurrences WHERE entry_id = ? AND rule_id IN (
                        SELECT id FROM commitments WHERE kind <> ? OR account_id <> ? OR destination_id IS NOT ?
                    )
                """, (item_id, kind, account_id, destination_id))
                return item_id
            return conn.execute("""
                INSERT INTO entries (kind, name, posted_on, amount, account_id, destination_id,
                    category_id, labels, note, statement_expected, request_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (*values, request_key)).lastrowid

    def scenario_list(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id, name, updated_at FROM scenarios ORDER BY updated_at DESC, id DESC"
            )]

    def scenario(self, item_id):
        with self.connection() as conn:
            row = row_by_id(conn, "scenarios", item_id)
            return dict(row, input=json.loads(row["input"]), result=json.loads(row["result"]))

    def save_scenario(self, name, scenario, result, item_id=None):
        values = (name, json.dumps(scenario), json.dumps(result))
        with self.connection(write=True) as conn:
            if item_id is None:
                return conn.execute("INSERT INTO scenarios (name, input, result) VALUES (?, ?, ?)", values).lastrowid
            row_by_id(conn, "scenarios", item_id)
            conn.execute("""
                UPDATE scenarios SET name = ?, input = ?, result = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
            """, (*values, item_id))
            return item_id

    def delete(self, table, item_id):
        try:
            with self.connection(write=True) as conn:
                row_by_id(conn, table, item_id)
                conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
        except sqlite3.IntegrityError as error:
            message = {
                "accounts": "This account is used by transactions or planning records. Remove those links first.",
                "categories": "This category is used by transactions, subcategories, or planning records. Remove those links first.",
            }[table]
            raise ApiError(message, status=409) from error
