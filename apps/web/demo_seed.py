"""Persist fictional records only in the provisioned sample databases."""
import json
from datetime import date

from .demo import sample_ledger
from .forecast import add_months, historical_cash, liquid_ids, occurrence_dates, plan_metadata, post_occurrence, simulate
from .reports import DAY, Window, month_start
from .risk import analyze, build_model
from .shared import _post_rule, _save_person, _save_rule, _schedule_settlement, _settle
from .storage import Ledger
from .values import ApiError, euros


def _seed_start(conn, role):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sample_seed (
            role TEXT PRIMARY KEY,
            seeded_on TEXT NOT NULL
        )
    """)
    existing = conn.execute("SELECT role FROM sample_seed").fetchone()
    if existing:
        if existing["role"] != role:
            raise ApiError("This sample workspace was provisioned for a different role.", status=409)
        return False
    if conn.execute("SELECT 1 FROM accounts WHERE id > 0 LIMIT 1").fetchone():
        raise ApiError("The sample workspace already contains local accounts.", status=409)
    return True


def _mark_seeded(conn, role, today):
    conn.execute("INSERT INTO sample_seed(role,seeded_on) VALUES (?,?)", (role, today.isoformat()))


def _account(conn, name, kind, opened, amount):
    return conn.execute("""
        INSERT INTO accounts(name,kind,opened_on,opening_balance) VALUES(?,?,?,?)
    """, (name, kind, opened.isoformat(), amount)).lastrowid


def _category(conn, name, kind, color, parent=None):
    return conn.execute("INSERT INTO categories(name,kind,parent_id,color) VALUES(?,?,?,?)",
                        (name, kind, parent, color)).lastrowid


def _entry(conn, kind, name, day, amount, account, *, category=None, target=None, labels=(), note=""):
    return conn.execute("""
        INSERT INTO entries(kind,name,posted_on,amount,account_id,destination_id,category_id,labels,note)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, (kind, name, day, amount, account, target, category, json.dumps(labels), note)).lastrowid


def _copy_ledger(conn, ledger):
    accounts = {
        row["id"]: _account(conn, row["name"], row["kind"], date.fromisoformat(row["opened_on"]), row["opening_balance"])
        for row in ledger.accounts
    }
    categories = {}
    for row in ledger.categories:
        categories[row["id"]] = _category(conn, row["name"], row["kind"], row["color"], categories.get(row["parent_id"]))
    for row in ledger.entries:
        _entry(conn, row["kind"], row["name"], row["posted_on"], row["amount"], accounts[row["account_id"]],
               category=categories.get(row["category_id"]), target=accounts.get(row["destination_id"]),
               labels=row["labels"], note=row["note"])
    return accounts, categories


def _partner_ledger(today):
    first = month_start(today, -11).isoformat()
    accounts = [
        {"id": 1, "name": "Partner checking", "kind": "checking", "opening_balance": 245_000, "opened_on": first},
        {"id": 2, "name": "Partner savings", "kind": "savings", "opening_balance": 620_000, "opened_on": first},
        {"id": 3, "name": "Partner index portfolio", "kind": "investment", "opening_balance": 465_000, "opened_on": first},
    ]
    categories = [
        {"id": key, "kind": kind, "name": name, "color": color, "parent_id": parent}
        for key, kind, name, color, parent in (
            (1, "income", "Employment", "mint", None),
            (2, "expense", "Household", "rose", None),
            (3, "expense", "Shopping", "sky", 2),
            (4, "expense", "Travel & transport", "amber", None),
            (5, "investment", "Index funds", "indigo", None),
            (6, "income", "Savings interest", "mint", None),
        )
    ]
    entries = []
    for offset in range(-11, 1):
        month = month_start(today, offset)
        n = offset + 11
        for day, kind, name, amount, category, target in (
            (1, "income", "Partner salary", 308_000, 1, None),
            (3, "expense", "Household costs", 102_500 + n * 90, 2, None),
            (5, "transfer", "Monthly savings", 36_000, None, 2),
            (7, "investment", "Index contribution", 27_500, 5, 3),
            (12, "expense", "Transport pass", 11_450 + n * 115, 4, None),
            (18, "expense", "Household shopping", 11_300 + (n % 4) * 1750, 3, None),
            (21, "expense", "Weekend itinerary", 45_000 if n % 3 == 0 else 16_000, 4, None),
        ):
            posted = month.replace(day=day)
            if posted <= today:
                entries.append({"id": len(entries) + 1, "kind": kind, "name": name,
                                "posted_on": posted.isoformat(), "amount": amount, "category_id": category,
                                "account_id": 1, "destination_id": target, "labels": ["sample"], "note": ""})
    return Ledger(accounts, categories, entries)


def _commitment(conn, name, kind, purpose, start, amount, account, *, category=None, target=None, end=None):
    return conn.execute("""
        INSERT INTO commitments(name,purpose,kind,amount,account_id,destination_id,category_id,start_on,end_on,cadence)
        VALUES(?,?,?,?,?,?,?,?,?,'monthly')
    """, (name, purpose, kind, amount, account, target, category, start.isoformat(),
          end.isoformat() if end else None)).lastrowid


def _planned_series(conn, today, account, schedules):
    start = month_start(today, -7)
    scheduled = []
    for day, kind, purpose, name, amount, category, target in schedules:
        anchor = start.replace(day=day)
        commitment = _commitment(conn, name, kind, purpose, anchor, amount, account, category=category, target=target)
        scheduled.append((commitment, anchor, name, kind, account, target))
    return scheduled


def _match_prior(conn, rules, today):
    for commitment, anchor, name, kind, account, target in rules:
        dates = {day.isoformat() for day in occurrence_dates(anchor, "monthly", anchor, today)}
        existing = conn.execute("""
            SELECT id, posted_on FROM entries WHERE name=? AND kind=? AND account_id=? AND destination_id IS ?
            AND posted_on>=? ORDER BY posted_on,id
        """, (name, kind, account, target, anchor.isoformat()))
        linked = set()
        for entry in existing:
            day = entry["posted_on"]
            if day in dates and day not in linked:
                conn.execute("INSERT INTO commitment_occurrences VALUES(?,?,?)", (commitment, day, entry["id"]))
                linked.add(day)


def _goal(conn, name, savings, target, earmarked, monthly, due):
    item = conn.execute("INSERT INTO goals(name,target,target_on) VALUES(?,?,?)", (name, target, due.isoformat())).lastrowid
    conn.execute("INSERT INTO goal_funding(goal_id,account_id,earmarked,monthly) VALUES(?,?,?,?)",
                 (item, savings, earmarked, monthly))


def _return(conn, database, name, instrument, payout, category, *, today, amount, principal, rate, start):
    commitment = _commitment(conn, name, "income", "return", start, amount, payout, category=category,
                             end=add_months(start, 12))
    conn.execute("""
        INSERT INTO investment_returns(commitment_id,instrument_id,product,mode,principal,rate)
        VALUES(?,?,'deposit','rate',?,?)
    """, (commitment, instrument, principal, rate))
    for day in occurrence_dates(start, "monthly", start, today):
        post_occurrence(database, {"rule_id": commitment, "date": day.isoformat()}, today, conn=conn)


def _shared(conn, today, account, category, *, group=False, partner=False):
    contact = _save_person(conn, {"name": "Jamie" if not partner else "Alex", "tag": "jamie" if not partner else "alex"})
    first = month_start(today, -1).replace(day=8)
    amount = 60_000 if group else 5_899 if not partner else 4_500
    title = "Shared weekend booking" if group else "Shared broadband" if not partner else "Shared sports membership"
    rule = _save_rule(conn, {
        "name": title, "amount": euros(amount), "first_on": first.isoformat(),
        "frequency": "once" if group else "monthly", "account_id": account,
        "participants": [contact], "category_id": category, "labels": ["sample", "shared"],
    }, today)
    _post_rule(conn, rule, {"due_on": first.isoformat(), "posted_on": first.isoformat()}, today)
    settled = 0 if group else 1_000
    if settled:
        _settle(conn, {"person_id": contact, "direction": "receive", "amount": euros(settled),
                       "account_id": account, "posted_on": (first + DAY).isoformat()}, today)
    _schedule_settlement(conn, {"person_id": contact, "account_id": account, "direction": "receive",
                                "amount": euros(amount // 2 - settled), "due_on": (today + 7 * DAY).isoformat()}, today)


def _reserve(conn, account, amount, available=None):
    conn.execute("INSERT INTO funding_account_policy(account_id,reserve,available_on) VALUES(?,?,?)",
                 (account, amount, available.isoformat() if available else None))


def _route(conn, source, target, days, today):
    conn.execute("INSERT INTO funding_routes(source_id,destination_id,days,approved_on) VALUES(?,?,?,?)",
                 (source, target, days, today.isoformat()))


def _risk_example(database, conn, as_of, name, purchase, reserve, *, history=False):
    assumptions = {
        "days": 120, "paths": 120, "seed": 42, "reserve": euros(reserve),
        "purchase_amount": euros(purchase), "purchase_on": (as_of + 35 * DAY).isoformat(),
        "income_pct": 5, "expense_pct": 8, "delay_pct": 25, "delay_days": 21,
        "shock_pct": 20, "shock_amount": "300.00",
        "history": history,
    }
    result = analyze(build_model(database, assumptions, as_of, conn=conn))
    conn.execute("INSERT INTO decision_runs(name,issued_on,assumptions,result) VALUES(?,?,?,?)",
                 (name, as_of.isoformat(), json.dumps(assumptions), json.dumps(result)))


def seed_profile(database, today, role):
    if role not in ("personal", "partner"):
        raise ValueError("Choose a known sample profile role.")
    with database.connection(write=True) as conn:
        if not _seed_start(conn, role):
            return False
        partner = role == "partner"
        accounts, categories = _copy_ledger(conn, _partner_ledger(today) if partner else sample_ledger(today))
        checking, savings, investment = (accounts[key] for key in (1, 2, 3))
        if partner:
            series = [
                (1, "income", "payday", "Partner salary", 308_000, categories[1], None),
                (3, "expense", "bill", "Household costs", 103_800, categories[2], None),
                (5, "transfer", "commitment", "Monthly savings", 36_000, None, savings),
                (7, "investment", "commitment", "Index contribution", 27_500, categories[5], investment),
                (12, "expense", "subscription", "Transport pass", 12_700, categories[4], None),
            ]
        else:
            series = [
                (1, "income", "payday", "Monthly salary", 444_000, categories[1], None),
                (2, "expense", "bill", "Apartment rent", 142_500, categories[5], None),
                (5, "investment", "commitment", "Index fund contribution", 60_000, categories[12], investment),
                (6, "transfer", "commitment", "Monthly savings", 55_000, None, savings),
                (7, "expense", "bill", "Phone and utilities", 24_000, categories[6], None),
                (15, "investment", "commitment", "Retirement contribution", 23_000, categories[13], investment),
            ]
        schedule = _planned_series(conn, today, checking, series)
        _risk_example(database, conn, month_start(today, -3).replace(day=16),
                      "Earlier cash forecast", 0, 300_000, history=True)
        _match_prior(conn, schedule, today)
        if partner:
            _goal(conn, "Weekend trips", savings, 160_000, 65_000, 25_000, add_months(today, 7))
            _reserve(conn, checking, 90_000)
            _reserve(conn, savings, 95_000)
            _shared(conn, today, checking, categories[3], partner=True)
        else:
            _goal(conn, "Emergency cushion", savings, 1_500_000, 450_000, 35_000, add_months(today, 21))
            _goal(conn, "Summer holiday", savings, 230_000, 80_000, 25_000, add_months(today, 9))
            _reserve(conn, checking, 450_000)
            _reserve(conn, savings, 120_000)
            _shared(conn, today, checking, categories[6])
            deposit = _account(conn, "Term deposit", "investment", month_start(today, -4), 500_000)
            _return(conn, database, "Term deposit coupon", deposit, checking, categories[14],
                    today=today, amount=1_500, principal=500_000, rate="0.3", start=month_start(today, -3).replace(day=20))
            _reserve(conn, deposit, 0, today + 65 * DAY)
            _route(conn, deposit, checking, 2, today)
        _route(conn, savings, checking, 1, today)
        _risk_example(database, conn, today, "Upcoming purchase check", 650_000, 500_000)
        _mark_seeded(conn, role, today)
        return True


def seed_family(database, workspace, today, *, handler=None):
    if workspace["kind"] != "group":
        raise ValueError("Family sample records require a group workspace.")
    with database.connection(write=True) as conn:
        if not _seed_start(conn, "family"):
            return False
        opened = month_start(today, -3)
        checking = _account(conn, "Family checking", "checking", opened, 180_000)
        savings = _account(conn, "Family reserve savings", "savings", opened, 520_000)
        residence = _category(conn, "Family household", "expense", "rose")
        costs = _category(conn, "Family bills", "expense", "amber", residence)
        shared = _category(conn, "Family outings", "expense", "violet", residence)
        income = _category(conn, "Family contributions", "income", "mint")
        _shared(conn, today, checking, shared, group=True)
        _commitment(conn, "Family utilities", "expense", "bill", today + 16 * DAY, 25_000, checking, category=costs)
        _commitment(conn, "Family contribution", "income", "payday", today + 18 * DAY, 37_500, checking, category=income)
        _goal(conn, "Shared family trip", savings, 360_000, 200_000, 14_000, add_months(today, 12))
        _reserve(conn, checking, 80_000)
        _reserve(conn, savings, 100_000)
        _route(conn, savings, checking, 1, today)
        for name, member, category, maximum in (
            ("Family spending", None, None, 380_000),
            ("Shared household expenses", None, residence, 45_000),
        ):
            conn.execute("""
                INSERT INTO family_limits(group_id,name,member_id,category_id,monthly_limit,starts_month)
                VALUES(?,?,?,?,?,?)
            """, (workspace["id"], name, member, category, maximum, month_start(today, -3).isoformat()[:7]))
        _risk_example(database, conn, today, "Family reserve outlook", 90_000, 500_000)
        _mark_seeded(conn, "family", today)
        return True


def _comparison_seeded(conn):
    return conn.execute("SELECT 1 FROM sample_comparisons_seeded WHERE id=1").fetchone() is not None


def _purchase_example(database, projection, issued):
    ledger = database.ledger()
    account_scope = liquid_ids(ledger)
    opening = historical_cash(ledger, [Window(issued, issued)], account_scope)[0]
    data = {
        "startingMonth": issued.isoformat()[:7], "startingCash": euros(opening),
        "monthlyNetCashFlow": "1450.00", "minimumReserve": "6000.00", "purchasePrice": "6200.00",
        "dueMonth": month_start(issued, 4).isoformat()[:7],
    }
    try:
        inputs, result = projection.compare(data)
    except ApiError as error:
        if error.status == 503:
            return None
        raise
    monthly_targets = {"Employment": "4440.00", "Rent": "1425.00", "Groceries": "285.00",
                       "Utilities": "240.00", "Index funds": "600.00", "Retirement": "230.00"}
    inputs.update(plan_metadata(database, {"varianceTargets": [
        {"category_id": row["id"], "amount": monthly_targets[row["name"]]}
        for row in ledger.categories if row["name"] in monthly_targets
    ]}))
    return inputs, result


def seed_comparisons(database, today, projection=None):
    """Save sample observations without replacing later edits or deleted examples."""
    with database.connection(write=True) as conn:
        marked = conn.execute("SELECT 1 FROM sqlite_master WHERE name='sample_seed' AND type='table'").fetchone()
        if not marked or not conn.execute("SELECT 1 FROM sample_seed WHERE role='personal'").fetchone():
            raise ApiError("Example comparisons require the sample personal workspace.", status=409)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sample_comparisons_seeded (
                id INTEGER PRIMARY KEY CHECK (id=1),
                seeded_on TEXT NOT NULL
            )
        """)
        if _comparison_seeded(conn):
            return False
        rental = conn.execute("""
            SELECT id FROM commitments WHERE name='Apartment rent' AND kind='expense' AND archived_on IS NULL
        """).fetchone()
    issued = month_start(today, -3).replace(day=16)
    assumptions = {
        "months": 7, "purchase_amount": "6200.00", "purchase_on": add_months(issued, 2).isoformat(),
        "purchase_shift": 1, "reserve": "6000.00", "investment_delta": "100.00",
    }
    if rental:
        assumptions.update(rent_rule=rental["id"], rent_percent=10)
    comparison = simulate(database, assumptions, issued)
    purchase = _purchase_example(database, projection, issued) if projection is not None else None
    with database.connection(write=True) as conn:
        if _comparison_seeded(conn):
            return False
        conn.execute("INSERT INTO forecast_comparisons(name,saved_on,inputs,result) VALUES(?,?,?,?)",
                     ("Delay the car purchase", issued.isoformat(), json.dumps(assumptions), json.dumps(comparison)))
        if purchase is not None:
            inputs, result = purchase
            conn.execute("INSERT INTO scenarios(name,input,result,updated_at) VALUES(?,?,?,?)",
                         ("Replace the car", json.dumps(inputs), json.dumps(result), issued.isoformat() + " 12:00:00"))
        conn.execute("INSERT INTO sample_comparisons_seeded VALUES(1,?)", (today.isoformat(),))
        return True
