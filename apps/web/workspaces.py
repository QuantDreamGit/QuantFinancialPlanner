"""Local profiles and group snapshots that preserve each ledger's ownership."""
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import closing, contextmanager, nullcontext
from threading import Lock

from .starter_categories import apply_starter_categories, starter_requested
from .storage import Database
from .values import ApiError, choice, identifier, line


STRIDE = 1_000_000_000
REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('profile', 'group'))
);
CREATE TABLE IF NOT EXISTS workspace_members (
    group_id INTEGER NOT NULL REFERENCES workspaces(id),
    profile_id INTEGER NOT NULL REFERENCES workspaces(id),
    PRIMARY KEY (group_id, profile_id)
);
INSERT OR IGNORE INTO workspaces(id, name, kind) VALUES (1, 'Personal', 'profile');
CREATE TABLE IF NOT EXISTS demo_workspaces (
    workspace_id INTEGER PRIMARY KEY REFERENCES workspaces(id),
    owner_profile_id INTEGER NOT NULL REFERENCES workspaces(id),
    role TEXT NOT NULL CHECK (role IN ('personal', 'partner', 'family')),
    ready INTEGER NOT NULL DEFAULT 0 CHECK (ready IN (0, 1)),
    UNIQUE(owner_profile_id, role)
);
"""
DEMO_NAMES = {"personal": "Sample workspace", "partner": "Sample partner", "family": "Sample Family"}
GROUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_catalog (
    kind TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    profile_name TEXT NOT NULL,
    PRIMARY KEY (kind, item_id)
);
CREATE TABLE IF NOT EXISTS group_shared_entries (
    id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    signature TEXT NOT NULL
);
"""


def owner_id(item_id):
    return -item_id // STRIDE if isinstance(item_id, int) and item_id <= -STRIDE else None


def group_id(profile, ident):
    if ident is None:
        return None
    if abs(ident) >= STRIDE // 2 or profile > (2**53 - STRIDE) // STRIDE:
        raise ApiError("This workspace reached its supported record ID range.")
    return -(profile * STRIDE + abs(ident) * 2 + int(ident < 0))


def local_id(ident):
    offset = -ident % STRIDE
    return (-1 if offset % 2 else 1) * (offset // 2)


def member_fields(member):
    return {"profile_id": member["id"], "profile_name": member["name"]}


def owned_account(ident, field="account_id"):
    if owner_id(ident):
        raise ApiError("Open this account’s personal workspace to record activity, or choose a joint account.", field)
    return ident


class WorkspaceRegistry:
    def __init__(self, primary):
        from .cars import initialize_registry_cars

        self.primary = primary
        self.folder = primary.path.parent / (primary.path.stem + ".workspaces")
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.folder / "registry.sqlite3"
        try:
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            pass
        self._lock = Lock()
        self._group_locks = {}
        self._databases = {1: primary}
        with self.connection() as conn:
            conn.executescript(REGISTRY_SCHEMA)
        initialize_registry_cars(self)

    @contextmanager
    def connection(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=8)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn

    def list(self, conn=None):
        with (nullcontext(conn) if conn is not None else self.connection()) as conn:
            rows = {row["id"]: dict(row, members=[], groups=[]) for row in
                    conn.execute("SELECT id, name, kind FROM workspaces ORDER BY id")}
            for record in conn.execute("SELECT * FROM demo_workspaces"):
                rows[record["workspace_id"]].update(
                    name=DEMO_NAMES[record["role"]], demo_role=record["role"],
                    demo_owner=record["owner_profile_id"], demo_ready=bool(record["ready"]),
                )
            for row in conn.execute("SELECT group_id, profile_id FROM workspace_members ORDER BY profile_id"):
                group, profile = rows[row["group_id"]], rows[row["profile_id"]]
                group["members"].append({"id": profile["id"], "name": profile["name"]})
                profile["groups"].append({"id": group["id"], "name": group["name"]})
        return list(rows.values())

    def provision_demo(self, profile_id, today, handler):
        from .demo_seed import seed_comparisons, seed_family, seed_profile

        with self.group_access(profile_id):
            with self.connection(write=True) as conn:
                found = list(conn.execute(
                    "SELECT role, workspace_id, ready FROM demo_workspaces WHERE owner_profile_id = ?", (profile_id,)
                ))
                ids = {row["role"]: row["workspace_id"] for row in found}
                if len(ids) == len(DEMO_NAMES) and all(row["ready"] for row in found):
                    return {role + "_id": key for role, key in ids.items()}
                for role in DEMO_NAMES:
                    if role in ids:
                        continue
                    name = f"[sample:{profile_id}:{role}:{secrets.token_hex(8)}]"
                    kind = "group" if role == "family" else "profile"
                    ids[role] = conn.execute("INSERT INTO workspaces(name,kind) VALUES (?,?)", (name, kind)).lastrowid
                    conn.execute("INSERT INTO demo_workspaces(workspace_id,owner_profile_id,role) VALUES (?,?,?)",
                                 (ids[role], profile_id, role))
                conn.executemany("INSERT OR IGNORE INTO workspace_members VALUES (?,?)",
                                 [(ids["family"], ids["personal"]), (ids["family"], ids["partner"])])
            for role in ("personal", "partner"):
                seed_profile(self.database(self.workspace(ids[role])), today, role)
            personal = self.database(self.workspace(ids["personal"]))
            projection = getattr(getattr(handler, "server", None), "projection", None)
            seed_comparisons(personal, today, projection)
            family = self.workspace(ids["family"])
            seed_family(self.database(family), family, today, handler=handler)
            with self.connection(write=True) as conn:
                conn.execute("UPDATE demo_workspaces SET ready = 1 WHERE owner_profile_id = ?", (profile_id,))
        return {role + "_id": key for role, key in ids.items()}

    def workspace(self, ident):
        row = next((item for item in self.list() if item["id"] == ident), None)
        if row is None:
            raise ApiError("Choose an existing workspace.", status=404)
        return row

    @contextmanager
    def group_access(self, ident):
        with self._lock:
            lock = self._group_locks.setdefault(ident, Lock())
        with lock:
            yield

    def _new_database(self, ident, kind):
        path = self.folder / (str(ident) + ".sqlite3")
        database = GroupDatabase(path, self, ident) if kind == "group" else Database(path)
        database.initialize()
        return database

    def database(self, workspace):
        ident = workspace["id"]
        with self._lock:
            database = self._databases.get(ident)
            if database is None:
                database = self._new_database(ident, workspace["kind"])
                self._databases[ident] = database
        if workspace["kind"] == "group":
            database.sync(workspace["members"])
        return database

    def save(self, payload, item_id=None):
        add_categories = starter_requested(payload)
        if item_id is not None and add_categories:
            raise ApiError("Apply starter categories from the Categories page of this workspace.", "starter_categories")
        name = line(payload.get("name"), "name", 65)
        kind = choice(payload.get("kind"), ("group", "profile"), "kind")
        selected = payload.get("members", [])
        if not isinstance(selected, list):
            raise ApiError("Choose the profiles to include.", "members")
        members = {identifier(value, "members") for value in selected}
        if None in members or (kind == "group" and len(members) < 2) or (kind == "profile" and members):
            raise ApiError("Choose at least two profiles for a group.", "members")
        new_database = None
        with self.connection(write=True) as conn:
            valid = {row[0] for row in conn.execute("""
                SELECT id FROM workspaces WHERE kind = 'profile'
                AND NOT EXISTS (SELECT 1 FROM demo_workspaces WHERE workspace_id = workspaces.id)
            """)}
            if not members.issubset(valid):
                raise ApiError("Only personal profiles can be group members.", "members")
            if conn.execute("SELECT 1 FROM workspaces WHERE name = ? AND id <> ?", (name, item_id or 0)).fetchone():
                raise ApiError("A workspace already uses this name.", "name", 409)
            if item_id is None:
                item_id = conn.execute("INSERT INTO workspaces(name, kind) VALUES (?,?)", (name, kind)).lastrowid
            else:
                old = conn.execute("""
                    SELECT kind FROM workspaces WHERE id = ?
                    AND NOT EXISTS (SELECT 1 FROM demo_workspaces WHERE workspace_id = workspaces.id)
                """, (item_id,)).fetchone()
                if not old or old["kind"] != kind:
                    raise ApiError("Choose an existing workspace of the same type.", status=404)
                removed = {row[0] for row in conn.execute(
                    "SELECT profile_id FROM workspace_members WHERE group_id = ?", (item_id,))} - members
                if removed and any(owner_id(account) in removed for account in self.group_goal_accounts(item_id)):
                    raise ApiError("Move the group goal’s funding before removing that member.", "members", 409)
                conn.execute("UPDATE workspaces SET name = ? WHERE id = ?", (name, item_id))
            conn.execute("DELETE FROM workspace_members WHERE group_id = ?", (item_id,))
            conn.executemany("INSERT INTO workspace_members VALUES (?,?)", [(item_id, ident) for ident in sorted(members)])
            if add_categories:
                new_database = self._new_database(item_id, kind)
                apply_starter_categories(new_database)
        if new_database is not None:
            with self._lock:
                self._databases.setdefault(item_id, new_database)
        return self.workspace(item_id)

    def group_goal_accounts(self, ident):
        path = self.folder / (str(ident) + ".sqlite3")
        if not path.exists():
            return []
        with Database(path).connection(snapshot=True) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'goal_funding'").fetchone():
                return []
            return [row[0] for row in conn.execute("SELECT account_id FROM goal_funding WHERE goal_id > 0")]


class GroupDatabase(Database):
    kind = "group"

    def __init__(self, path, registry, ident):
        super().__init__(path)
        self.registry = registry
        self.ident = ident
        self._sync = Lock()

    def initialize(self):
        super().initialize()
        with self.connection() as conn:
            conn.executescript(GROUP_SCHEMA)

    def sources(self, members):
        from .forecast import rules_and_links
        from .goals import load_goals
        records = []
        for member in members:
            database = self.registry.database(dict(member, kind="profile"))
            with database.connection(snapshot=True) as conn:
                ledger = database.ledger(conn)
                rules, _ = rules_and_links(database, conn=conn)
                links = [list(row) for row in conn.execute(
                    "SELECT rule_id,due_on,entry_id FROM commitment_occurrences ORDER BY rule_id,due_on")]
                goals = load_goals(conn)
                returns = [dict(row) for row in conn.execute("SELECT * FROM investment_returns ORDER BY id")]
            records.append((member, ledger.accounts, ledger.categories, ledger.entries, rules, links, goals, returns))
        return records

    def sync(self, members):
        with self._sync:
            sources = self.sources(members)
            signature = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
            with self.connection() as conn:
                current = conn.execute("SELECT signature FROM group_snapshot WHERE id=1").fetchone()
                if current and current[0] == signature:
                    return
            with self.connection(write=True) as conn:
                conn.execute("PRAGMA defer_foreign_keys = ON")
                for statement in (
                    "DELETE FROM commitment_occurrences WHERE rule_id < 0",
                    "DELETE FROM commitments WHERE id < 0",
                    "DELETE FROM entries WHERE id < 0",
                    "DELETE FROM goals WHERE id < 0",
                    "DELETE FROM group_catalog",
                    "DELETE FROM group_shared_entries",
                ):
                    conn.execute(statement)
                for source in sources:
                    self.copy_profile(conn, *source)
                conn.execute("INSERT INTO group_snapshot VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET signature=excluded.signature",
                             (signature,))

    def copy_profile(self, conn, member, accounts, categories, entries, rules, links, goals, returns):
        profile = member["id"]

        def key(ident):
            return group_id(profile, ident)

        def named(kind, row, parent=None):
            name = row["name"] + " · " + member["name"] if kind == "accounts" or parent is None else row["name"]
            # Keep collisions with joint account names or existing group labels unambiguous.
            if kind == "accounts":
                collision = conn.execute("SELECT id FROM accounts WHERE name = ? AND id <> ?", (name, key(row["id"]))).fetchone()
            else:
                collision = conn.execute("SELECT id FROM categories WHERE name = ? AND id <> ? AND kind = ? AND parent_id IS ?",
                                         (name, key(row["id"]), row["kind"], parent)).fetchone()
            if collision:
                name += f" [{profile}/{row['id']}]"
            conn.execute("INSERT INTO group_catalog VALUES (?,?,?)", (kind, key(row["id"]), member["name"]))
            return name

        for item in accounts:
            name = named("accounts", item)
            conn.execute("""
                INSERT INTO accounts VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,
                    opened_on=excluded.opened_on,opening_balance=excluded.opening_balance
            """, (key(item["id"]), name, item["kind"], item["opened_on"], item["opening_balance"]))
        for item in categories:
            parent = key(item["parent_id"])
            name = named("categories", item, parent)
            conn.execute("""
                INSERT INTO categories(id,name,kind,parent_id,color) VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,parent_id=excluded.parent_id,color=excluded.color
            """, (key(item["id"]), name, item["kind"], parent, item["color"]))
        for entry in entries:
            item = dict(entry, id=key(entry["id"]), **member_fields(member))
            for field in ("account_id", "destination_id", "category_id"):
                item[field] = key(entry[field])
            if entry.get("shared"):
                conn.execute("INSERT INTO group_shared_entries VALUES (?,?)", (item["id"], json.dumps(item)))
            else:
                conn.execute("""
                    INSERT INTO entries(id,kind,name,posted_on,amount,account_id,destination_id,category_id,labels,note,
                                        statement_expected)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """, tuple(item[field] for field in ("id","kind","name","posted_on","amount","account_id","destination_id","category_id"))
                    + (json.dumps(item["labels"]), item["note"], item["statement_expected"]))
        for item in rules:
            conn.execute("""
                INSERT INTO commitments(id,name,purpose,kind,amount,account_id,destination_id,category_id,
                                        start_on,end_on,cadence,archived_on) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """, (key(item["id"]), item["name"], item["purpose"], item["kind"], item["amount"],
                  key(item["account_id"]), key(item["destination_id"]), key(item["category_id"]),
                  item["start_on"], item["end_on"], item["cadence"], item["archived_on"]))
        conn.executemany("INSERT INTO commitment_occurrences VALUES (?,?,?)",
                         [(key(rule), due, key(entry)) for rule, due, entry in links])
        conn.executemany("""
            INSERT INTO investment_returns (id,commitment_id,instrument_id,product,mode,principal,rate)
            VALUES (?,?,?,?,?,?,?)
        """, [(key(item["id"]), key(item["commitment_id"]), key(item["instrument_id"]), item["product"],
               item["mode"], item["principal"], item["rate"]) for item in returns])
        for goal in goals:
            name = goal["name"] + " · " + member["name"]
            if conn.execute("SELECT 1 FROM goals WHERE name = ?", (name,)).fetchone():
                name += f" [{profile}/{goal['id']}]"
            conn.execute("INSERT INTO goals(id,name,target,target_on) VALUES (?,?,?,?)",
                         (key(goal["id"]), name, goal["target"], goal["target_on"]))
            conn.executemany("INSERT INTO goal_funding VALUES (?,?,?,?)", [
                (key(goal["id"]), key(link["account_id"]), link["earmarked"], link["monthly"])
                for link in goal["funding"]
            ])

    def ledger(self, conn=None):
        with (nullcontext(conn) if conn is not None else self.connection(snapshot=True)) as conn:
            ledger = super().ledger(conn)
            catalog = {(row["kind"], row["item_id"]): row["profile_name"] for row in conn.execute("SELECT * FROM group_catalog")}
            people = {owner_id(ident): name for (_, ident), name in catalog.items()}

            def active(items, kind):
                return [dict(item, profile_id=owner_id(item["id"]), profile_name=catalog[kind, item["id"]])
                        if owner_id(item["id"]) else item for item in items
                        if item["id"] > 0 or (kind, item["id"]) in catalog]

            ledger.accounts = active(ledger.accounts, "accounts")
            ledger.categories = active(ledger.categories, "categories")
            for item in ledger.entries:
                profile = owner_id(item["id"])
                if profile:
                    item.update(profile_id=profile, profile_name=people.get(profile, "Member"))
            ledger.entries.extend(json.loads(row[0]) for row in conn.execute("SELECT payload FROM group_shared_entries"))
            ledger.entries.sort(key=lambda item: (item["posted_on"], item["id"]))
            return ledger

    def save_entry(self, data, today, item_id=None, *, conn=None):
        source = identifier(data.get("account_id"), "account_id")
        target = identifier(data.get("destination_id"), "destination_id")
        category = identifier(data.get("category_id"), "category_id")
        profile = owner_id(source)
        if profile:
            if item_id is not None or conn is not None or target is not None and owner_id(target) != profile:
                raise ApiError("Record this transaction in its account’s personal workspace.", "account_id")
            if category is not None and owner_id(category) != profile:
                raise ApiError("Choose a category from the same personal workspace.", "category_id")
            workspace = self.registry.workspace(self.ident)
            member = next((member for member in workspace["members"] if member["id"] == profile), None)
            if member is None:
                raise ApiError("This account is outside the current group.", "account_id")
            account = self.registry.database(dict(member, kind="profile"))
            values = dict(data, account_id=local_id(source), destination_id=local_id(target) if target else None,
                          category_id=local_id(category) if category else None)
            return group_id(profile, account.save_entry(values, today))
        owned_account(target, "destination_id")
        owned_account(category, "category_id")
        return super().save_entry(data, today, item_id, conn=conn)

    def save_category(self, data, item_id=None):
        owned_account(identifier(data.get("parent_id"), "parent_id"), "parent_id")
        return super().save_category(data, item_id)

    def member_shared_cash(self, start, end, conn):
        from .shared import projected_cash
        active = {row[0] for row in conn.execute("SELECT item_id FROM group_catalog WHERE kind='accounts'")}
        results = []
        for member in self.registry.workspace(self.ident)["members"]:
            database = self.registry.database(dict(member, kind="profile"))
            for event in projected_cash(database, start, end):
                values = {field: group_id(member["id"], event[field]) for field in
                          ("account_id", "category_id", "rule_id", "scheduled_settlement_id") if field in event}
                if values["account_id"] in active:
                    results.append(dict(event, **values, **member_fields(member)))
        return results

    def member_shared_rules(self, conn):
        from .shared import recurring_cash_rules
        active = {owner_id(row[0]) for row in conn.execute("SELECT item_id FROM group_catalog WHERE kind='accounts'")}
        results = []
        for member in self.registry.workspace(self.ident)["members"]:
            if member["id"] in active:
                database = self.registry.database(dict(member, kind="profile"))
                results.extend(dict(rule, id=group_id(member["id"], rule["id"]),
                                    name=rule["name"] + " · " + member["name"])
                               for rule in recurring_cash_rules(database))
        return results
