"""Account reserves and approved directions for advisory funding plans."""
import re
from collections import defaultdict
from contextlib import nullcontext
from datetime import date

from .values import ApiError, identifier, money
from .workspaces import local_id, owner_id


LIQUID = frozenset(("checking", "savings", "cash"))
BASE = "/api/funding"
SCHEMA = """
CREATE TABLE IF NOT EXISTS funding_account_policy (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE CHECK (account_id > 0),
    reserve INTEGER NOT NULL CHECK (reserve >= 0),
    available_on TEXT
);
CREATE TABLE IF NOT EXISTS funding_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    destination_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    days INTEGER NOT NULL CHECK (days BETWEEN 0 AND 14),
    approved_on TEXT NOT NULL,
    CHECK (source_id <> destination_id),
    UNIQUE (source_id, destination_id)
);
"""


def initialize_funding(database):
    with database.connection() as conn:
        conn.executescript(SCHEMA)


def _workspace(handler, database):
    if workspace := getattr(handler, "workspace", None):
        return workspace
    if getattr(database, "kind", None) == "group":
        return database.registry.workspace(database.ident)
    return {"kind": "profile", "id": None, "members": []}


def _verify_workspace(handler, workspace):
    ensure = getattr(handler, "can_open_workspace", None)
    if callable(ensure):
        if not ensure(workspace):
            raise ApiError("You don’t have access to this workspace.", status=403)
    elif getattr(handler, "user", None):
        who = handler.user["profile_id"]
        members = {member["id"] for member in workspace.get("members", [])}
        if workspace["id"] not in (None, who) and who not in members:
            raise ApiError("You don’t have access to this workspace.", status=403)


def _can_edit(handler, ident):
    owner = owner_id(ident)
    if owner is None:
        # Positive accounts belong to the already authorized active workspace.
        return True
    can_use = getattr(handler, "can_use_personal_profile", None)
    if callable(can_use):
        return bool(can_use(owner))
    user = getattr(handler, "user", None)
    return bool(user and user["profile_id"] == owner)


def _member_database(workspace_db, workspace, owner):
    member = next((person for person in workspace.get("members", []) if person["id"] == owner), None)
    registry = getattr(workspace_db, "registry", None)
    if member is None or registry is None:
        raise ApiError("Choose an account visible in this workspace.", "account_id", 404)
    return registry.database(dict(member, kind="profile"))


def _policy_rows(conn):
    return {row["account_id"]: (row["reserve"], row["available_on"]) for row in
            conn.execute("SELECT account_id,reserve,available_on FROM funding_account_policy")}


def _account_policies(workspace_db, workspace, accounts, conn):
    policies = _policy_rows(conn)
    inherited = defaultdict(list)
    for ident in accounts:
        if owner := owner_id(ident):
            inherited[owner].append(ident)
    for owner, identities in inherited.items():
        database = _member_database(workspace_db, workspace, owner)
        with database.connection(snapshot=True) as owner_conn:
            member_policies = _policy_rows(owner_conn)
        for ident in identities:
            if local_id(ident) in member_policies:
                policies[ident] = member_policies[local_id(ident)]
    return policies


def _route_active(source, destination, available_on):
    return destination["kind"] in LIQUID and (source["kind"] in LIQUID or
                                             (source["kind"] == "investment" and available_on is not None))


def funding_policy(handler, workspace_db, ledger, *, conn=None):
    """Read visible rules and member reserves without inheriting routes from other workspaces."""
    workspace = _workspace(handler, workspace_db)
    _verify_workspace(handler, workspace)
    accounts = {account["id"]: account for account in ledger.accounts}
    with (nullcontext(conn) if conn is not None else workspace_db.connection(snapshot=True)) as connection:
        policies = _account_policies(workspace_db, workspace, accounts, connection)
        routes = []
        for route in connection.execute("SELECT * FROM funding_routes ORDER BY id"):
            source = accounts.get(route["source_id"])
            destination = accounts.get(route["destination_id"])
            if source is None or destination is None:
                continue
            _, available = policies.get(route["source_id"], (0, None))
            routes.append(dict(route, can_edit=_can_edit(handler, route["source_id"]),
                               active=_route_active(source, destination, available)))
    output = []
    for ident, account in accounts.items():
        reserve, available = policies.get(ident, (0, None))
        visible = {key: account[key] for key in ("id", "name", "kind", "opened_on", "profile_id", "profile_name")
                   if key in account}
        output.append(dict(visible, reserve=str(reserve), available_on=available, can_edit=_can_edit(handler, ident)))
    return {"accounts": output, "routes": routes}


def _available_date(value, opened_on):
    if value is None or value == "":
        return None
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value and value >= opened_on:
            return value
    except (TypeError, ValueError):
        pass
    raise ApiError("Choose a date on or after this account opened.", "available_on")


def _account(accounts, ident, field):
    account = accounts.get(ident)
    if account is None:
        raise ApiError("Choose an account visible in this workspace.", field, 404)
    return account


def _require_owner(handler, ident, field):
    if not _can_edit(handler, ident):
        raise ApiError("Only this account’s owner can change its funding permissions.", field, 403)


def _save_account_policy(handler, database, workspace, account_id, data):
    reserve = money(data.get("reserve"), "reserve")
    if reserve < 0:
        raise ApiError("Enter an account reserve of €0 or more.", "reserve")
    # Check visibility before resolving the physical owner database.
    account = _account({item["id"]: item for item in database.ledger().accounts}, account_id, "account_id")
    _require_owner(handler, account_id, "account_id")
    owner = owner_id(account_id)
    target = _member_database(database, workspace, owner) if owner else database
    ident = local_id(account_id) if owner else account_id
    with target.connection(write=True) as conn:
        account = conn.execute("SELECT opened_on FROM accounts WHERE id = ?", (ident,)).fetchone()
        if account is None:
            raise ApiError("Choose an account visible in this workspace.", "account_id", 404)
        available = _available_date(data.get("available_on"), account["opened_on"])
        conn.execute("""
            INSERT INTO funding_account_policy(account_id,reserve,available_on) VALUES (?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET reserve=excluded.reserve, available_on=excluded.available_on
        """, (ident, reserve, available))
    return {"id": account_id, "reserve": str(reserve), "available_on": available}


def _delay(data):
    days = data.get("days")
    if type(days) is not int or not 0 <= days <= 14:
        raise ApiError("Choose a whole number from 0 to 14 business days.", "days")
    return days


def _check_route(handler, source, destination, settings):
    _require_owner(handler, source["id"], "source_id")
    if source["id"] == destination["id"]:
        raise ApiError("Choose two different accounts.", "destination_id")
    if destination["kind"] not in LIQUID:
        raise ApiError("Use a checking, savings or cash account as the destination.", "destination_id")
    _, available = settings.get(source["id"], (0, None))
    if not _route_active(source, destination, available):
        raise ApiError("Set a maturity date before offering funds from an investment account.", "source_id")


def _save_route(handler, database, workspace, data, today, route_id=None):
    days = _delay(data)
    with database.connection(write=True) as conn:
        accounts = {item["id"]: item for item in database.ledger(conn).accounts}
        if route_id is None:
            source_id = identifier(data.get("source_id"), "source_id")
            destination_id = identifier(data.get("destination_id"), "destination_id")
        else:
            previous = conn.execute("SELECT * FROM funding_routes WHERE id = ?", (route_id,)).fetchone()
            if previous is None:
                raise ApiError("This funding route is unavailable.", status=404)
            source_id, destination_id = previous["source_id"], previous["destination_id"]
            if "source_id" in data or "destination_id" in data:
                raise ApiError("Create a new route to change the source or destination.")
        source = _account(accounts, source_id, "source_id")
        destination = _account(accounts, destination_id, "destination_id")
        # Read source policy only once, even when the group contains many accounts.
        settings = _account_policies(database, workspace, {source_id: source}, conn)
        _check_route(handler, source, destination, settings)
        if route_id is None:
            previous = conn.execute("""
                SELECT id,days FROM funding_routes WHERE source_id=? AND destination_id=?
            """, (source_id, destination_id)).fetchone()
            if previous is not None:
                if previous["days"] == days:
                    return previous["id"]
                raise ApiError("This transfer direction has already been approved. Edit its delay instead.", "source_id", 409)
            return conn.execute("""
                INSERT INTO funding_routes(source_id,destination_id,days,approved_on) VALUES (?,?,?,?)
            """, (source_id, destination_id, days, today.isoformat())).lastrowid
        conn.execute("UPDATE funding_routes SET days=?, approved_on=? WHERE id=?", (days, today.isoformat(), route_id))
        return route_id


def _remove_route(handler, database, route_id):
    with database.connection(write=True) as conn:
        route = conn.execute("SELECT source_id FROM funding_routes WHERE id = ?", (route_id,)).fetchone()
        if route is None:
            raise ApiError("This funding route is unavailable.", status=404)
        source_id = route["source_id"]
        _account({item["id"]: item for item in database.ledger(conn).accounts}, source_id, "source_id")
        _require_owner(handler, source_id, "source_id")
        conn.execute("DELETE FROM funding_routes WHERE id = ?", (route_id,))
    return {"id": route_id}


def handle_funding_policy(handler, path, method, today):
    if path == BASE + "/policy":
        route, setting = None, None
    else:
        setting = re.fullmatch(BASE + r"/accounts/(-?[1-9][0-9]{0,18})", path)
        route = re.fullmatch(BASE + r"/routes(?:/([1-9][0-9]{0,18}))?", path)
        if setting is None and route is None:
            return None

    database = getattr(handler, "database", None) or handler.server.database
    workspace = _workspace(handler, database)
    _verify_workspace(handler, workspace)
    if path == BASE + "/policy" and method == "GET":
        with database.connection(snapshot=True) as conn:
            return funding_policy(handler, database, database.ledger(conn), conn=conn), 200
    if setting is not None and method == "PUT":
        ident = identifier(setting[1], "account_id")
        return _save_account_policy(handler, database, workspace, ident, handler.read_data(4096)), 200
    if route is not None:
        ident = identifier(route[1], "route") if route[1] else None
        if method == "POST" and ident is None or method == "PUT" and ident is not None:
            result = _save_route(handler, database, workspace, handler.read_data(4096), today, ident)
            return {"id": result}, 201 if ident is None else 200
        if method == "DELETE" and ident is not None:
            return _remove_route(handler, database, ident), 200
    raise ApiError("This funding policy action is unavailable.", status=405)
