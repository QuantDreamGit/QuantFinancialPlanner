"""Vehicle running costs, odometer-based service intervals and condition records."""
import csv
import io
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction
from math import ceil

from .storage import row_by_id
from .values import ApiError, choice, identifier, line, money, recorded_date


DRIVETRAINS = {
    "fwd": "2WD · Front-wheel drive",
    "rwd": "2WD · Rear-wheel drive",
    "4wd": "4WD / AWD",
}
VIEWS = {"exterior": "Exterior", "engine": "Engine bay", "underbody": "Underbody"}
# Part IDs are stable keys for saved inspections.
PARTS = (
    ("front_bumper", "Front bumper", "exterior", 50, 13),
    ("headlights", "Headlights", "exterior", 68, 22),
    ("front_left", "Front left tyre & wheel", "exterior", 26, 32),
    ("front_right", "Front right tyre & wheel", "exterior", 74, 32),
    ("glass", "Windows & windscreen", "exterior", 50, 37),
    ("body", "Bodywork & doors", "exterior", 36, 55),
    ("rear_left", "Rear left tyre & wheel", "exterior", 26, 70),
    ("rear_right", "Rear right tyre & wheel", "exterior", 74, 70),
    ("tail_lights", "Tail lights", "exterior", 68, 80),
    ("rear_bumper", "Rear bumper", "exterior", 50, 88),
    ("spare", "Spare wheel", "exterior", 50, 75),
    ("cooling", "Radiator & cooling", "engine", 50, 18),
    ("battery", "Battery", "engine", 75, 38),
    ("engine", "Engine", "engine", 50, 49),
    ("air_intake", "Air intake & filter", "engine", 25, 38),
    ("belts", "Accessory belts", "engine", 36, 64),
    ("brake_fluid", "Brake fluid & reservoir", "engine", 75, 69),
    ("turbo", "Turbocharger", "engine", 62, 70),
    ("front_brakes", "Front brakes", "underbody", 72, 28),
    ("front_suspension", "Front suspension", "underbody", 28, 28),
    ("gearbox", "Transmission", "underbody", 50, 34),
    ("front_diff", "Front differential", "underbody", 50, 21),
    ("transfer", "Transfer case / AWD coupling", "underbody", 50, 44),
    ("shaft", "Propeller shaft", "underbody", 50, 56),
    ("exhaust", "Exhaust", "underbody", 66, 54),
    ("chassis", "Underbody & chassis", "underbody", 33, 55),
    ("rear_diff", "Rear differential", "underbody", 50, 70),
    ("rear_brakes", "Rear brakes", "underbody", 72, 75),
    ("rear_suspension", "Rear suspension", "underbody", 28, 75),
    ("tow_hitch", "Tow hitch", "underbody", 50, 91),
)
EQUIPMENT = ("spare", "turbo", "tow_hitch")
DRIVETRAIN_PARTS = {
    "front_diff": ("fwd", "4wd"),
    "rear_diff": ("rwd", "4wd"),
    "shaft": ("rwd", "4wd"),
    "transfer": ("4wd",),
}
CONDITIONS = ("unchecked", "good", "watch", "repair")
ACTIVITY_KINDS = ("odometer", "fuel", "repair", "maintenance", "service")
MAX_KM = 10_000_000
MAX_CSV_ROWS = 1000
DRIVING_LOOKBACK = 90
FORECAST_DAYS = 365
BASE = "/api/cars"
ROUTE = re.compile(
    rf"{BASE}(?:/([1-9][0-9]{{0,17}})(?:/(activities|schedules|parts|odometer-import)"
    r"(?:/([a-z0-9_]{1,32}))?)?)?"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    drivetrain TEXT NOT NULL CHECK (drivetrain IN ('fwd', 'rwd', '4wd')),
    equipment TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS car_assignments (
    car_id INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    workspace_id INTEGER NOT NULL CHECK (workspace_id > 0),
    PRIMARY KEY (car_id, workspace_id)
);
CREATE INDEX IF NOT EXISTS car_assignments_workspace ON car_assignments (workspace_id, car_id);
CREATE TABLE IF NOT EXISTS car_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    interval_km INTEGER NOT NULL CHECK (interval_km > 0),
    baseline_km INTEGER NOT NULL CHECK (baseline_km >= 0),
    estimate INTEGER CHECK (estimate >= 0)
);
CREATE TABLE IF NOT EXISTS car_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('odometer', 'fuel', 'repair', 'maintenance', 'service')),
    recorded_on TEXT NOT NULL,
    odometer_km INTEGER NOT NULL CHECK (odometer_km >= 0),
    name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    cost INTEGER CHECK (cost >= 0),
    fuel_ml INTEGER CHECK (fuel_ml > 0),
    full_tank INTEGER NOT NULL DEFAULT 0 CHECK (full_tank IN (0, 1)),
    schedule_id INTEGER REFERENCES car_schedules(id) ON DELETE SET NULL,
    CHECK (
        (kind = 'odometer' AND cost IS NULL AND fuel_ml IS NULL AND full_tank = 0)
        OR (kind = 'fuel' AND cost IS NOT NULL AND fuel_ml IS NOT NULL)
        OR (kind IN ('repair', 'maintenance', 'service') AND fuel_ml IS NULL AND full_tank = 0)
    ),
    CHECK (schedule_id IS NULL OR kind = 'service')
);
CREATE INDEX IF NOT EXISTS car_activity_mileage ON car_activity (car_id, recorded_on, odometer_km);
CREATE INDEX IF NOT EXISTS car_schedules_car ON car_schedules (car_id);
CREATE TABLE IF NOT EXISTS car_inspections (
    car_id INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    part TEXT NOT NULL,
    condition TEXT NOT NULL CHECK (condition IN ('unchecked', 'good', 'watch', 'repair')),
    estimate INTEGER CHECK (estimate >= 0),
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (car_id, part)
);
"""


def initialize_cars(database):
    with database.connection() as conn:
        conn.executescript(SCHEMA)


def initialize_registry_cars(registry):
    """Import older workspace-local cars and their history into the shared catalog once."""
    from .storage import Database

    with registry.connection() as conn:
        conn.executescript(SCHEMA + """
            CREATE TABLE IF NOT EXISTS car_catalog_migration (id INTEGER PRIMARY KEY CHECK (id = 1));
        """)
    with registry.connection(write=True) as conn:
        if conn.execute("SELECT 1 FROM car_catalog_migration").fetchone():
            return
        for workspace in registry.list(conn):
            workspace_id = workspace["id"]
            path = registry.primary.path if workspace_id == 1 else registry.folder / f"{workspace_id}.sqlite3"
            if path.is_file():
                with Database(path).connection(snapshot=True) as source:
                    if source.execute("SELECT 1 FROM sqlite_master WHERE name = 'cars' AND type = 'table'").fetchone():
                        copy_workspace_cars(source, conn, workspace_id)
        conn.execute("INSERT INTO car_catalog_migration VALUES (1)")


def copy_workspace_cars(source, target, workspace_id):
    for car in source.execute("SELECT id, name, drivetrain, equipment FROM cars ORDER BY id"):
        car_id = target.execute("INSERT INTO cars (name, drivetrain, equipment) VALUES (?, ?, ?)",
                                (car["name"], car["drivetrain"], car["equipment"])).lastrowid
        target.execute("INSERT INTO car_assignments VALUES (?, ?)", (car_id, workspace_id))
        schedules = {}
        for row in source.execute("""
            SELECT id, name, interval_km, baseline_km, estimate FROM car_schedules WHERE car_id = ? ORDER BY id
        """, (car["id"],)):
            schedules[row["id"]] = target.execute("""
                INSERT INTO car_schedules (car_id, name, interval_km, baseline_km, estimate) VALUES (?, ?, ?, ?, ?)
            """, (car_id, *tuple(row)[1:])).lastrowid
        activity = source.execute("""
            SELECT recorded_on, odometer_km, kind, name, note, cost, fuel_ml, full_tank, schedule_id
            FROM car_activity WHERE car_id = ? ORDER BY id
        """, (car["id"],))
        target.executemany("""
            INSERT INTO car_activity (car_id, recorded_on, odometer_km, kind, name, note,
                                      cost, fuel_ml, full_tank, schedule_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ((car_id, *tuple(row)[:-1], schedules.get(row["schedule_id"])) for row in activity))
        parts = source.execute("SELECT part, condition, estimate, note FROM car_inspections WHERE car_id = ?", (car["id"],))
        target.executemany("""
            INSERT INTO car_inspections (car_id, part, condition, estimate, note) VALUES (?, ?, ?, ?, ?)
        """, ((car_id, *row) for row in parts))


def cents(value):
    return None if value is None else str(value)


def cost_field(value, field, *, required=False):
    if value in ("", None) and not required:
        return None
    amount = money(value, field)
    if amount < 0:
        raise ApiError("Enter a cost of €0 or more.", field)
    return amount


def kilometres(value, field, minimum=0):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,8}", value):
        value = int(value)
    if type(value) is int and minimum <= value <= MAX_KM:
        return value
    description = "a positive whole number" if minimum else "a non-negative whole number"
    raise ApiError(f"Enter {description} of kilometres up to 10,000,000.", field)


def litres(value):
    text = value.strip() if isinstance(value, str) else ""
    match = re.fullmatch(r"([0-9]{1,4})(?:[.,]([0-9]{1,3}))?", text)
    if match:
        whole, fraction = match.groups()
        ml = int(whole) * 1000 + int((fraction or "").ljust(3, "0"))
        if 0 < ml <= 1_000_000:
            return ml
    raise ApiError("Enter more than 0 and up to 1,000 litres, with up to three decimals.", "litres")


def car_payload(row):
    return dict(row, equipment=json.loads(row["equipment"]))


def equipped_parts(car):
    drivetrain = car["drivetrain"]
    equipment = car["equipment"]
    return {
        key: {"id": key, "name": name, "view": view, "x": x, "y": y}
        for key, name, view, x, y in PARTS
        if (key not in EQUIPMENT or key in equipment)
        and (key not in DRIVETRAIN_PARTS or drivetrain in DRIVETRAIN_PARTS[key])
    }


def owned_row(conn, table, car_id, record_id):
    row = conn.execute(f"SELECT * FROM {table} WHERE car_id = ? AND id = ?", (car_id, record_id)).fetchone()
    if row is None:
        raise ApiError("This car record no longer exists.", status=404)
    return row


def save_car(conn, data, car_id=None):
    name = line(data.get("name"), "name", 80)
    drivetrain = choice(data.get("drivetrain"), DRIVETRAINS, "drivetrain")
    equipment = data.get("equipment", [])
    if not isinstance(equipment, list) or len(equipment) > len(EQUIPMENT):
        raise ApiError("Choose from the listed equipment.", "equipment")
    selected = {choice(item, EQUIPMENT, "equipment") for item in equipment}
    values = (name, drivetrain, json.dumps([key for key in EQUIPMENT if key in selected]))
    if car_id is None:
        return conn.execute("INSERT INTO cars (name, drivetrain, equipment) VALUES (?, ?, ?)", values).lastrowid
    conn.execute("UPDATE cars SET name = ?, drivetrain = ?, equipment = ? WHERE id = ?", (*values, car_id))
    return car_id


def save_schedule(conn, car_id, data, record_id):
    values = (
        line(data.get("name"), "name", 80),
        kilometres(data.get("interval_km"), "interval_km", 1),
        kilometres(data.get("baseline_km"), "baseline_km"),
        cost_field(data.get("estimate"), "estimate"),
    )
    if record_id is None:
        return conn.execute("""
            INSERT INTO car_schedules (car_id, name, interval_km, baseline_km, estimate)
            VALUES (?, ?, ?, ?, ?)
        """, (car_id, *values)).lastrowid
    owned_row(conn, "car_schedules", car_id, record_id)
    conn.execute("""
        UPDATE car_schedules SET name = ?, interval_km = ?, baseline_km = ?, estimate = ? WHERE id = ?
    """, (*values, record_id))
    return record_id


def check_mileage(conn, car_id, day, odometer_km, record_id=None):
    previous_km, following_km = conn.execute("""
        SELECT MAX(CASE WHEN recorded_on < ? THEN odometer_km END),
               MIN(CASE WHEN recorded_on > ? THEN odometer_km END)
        FROM car_activity WHERE car_id = ? AND id <> ? AND recorded_on <> ?
    """, (day, day, car_id, record_id or 0, day)).fetchone()
    if previous_km is not None and odometer_km < previous_km:
        raise ApiError(f"The odometer must be at least {previous_km:,} km based on earlier readings.", "odometer_km")
    if following_km is not None and odometer_km > following_km:
        raise ApiError(f"The odometer must be at most {following_km:,} km based on later readings.", "odometer_km")


def save_activity(conn, car_id, data, today, record_id):
    old = owned_row(conn, "car_activity", car_id, record_id) if record_id is not None else None
    kind = old["kind"] if old else choice(data.get("kind"), ACTIVITY_KINDS, "kind")
    day = recorded_date(data.get("recorded_on"), "recorded_on", today)
    odometer = kilometres(data.get("odometer_km"), "odometer_km")
    note = line(data.get("note", ""), "note", 500, required=False)
    check_mileage(conn, car_id, day, odometer, record_id)
    name, cost, fuel_ml, full_tank, schedule_id = "", None, None, False, None
    if kind == "fuel":
        cost = cost_field(data.get("cost"), "cost", required=True)
        fuel_ml = litres(data.get("litres"))
        full_tank = data.get("full_tank", False)
        if type(full_tank) is not bool:
            raise ApiError("Choose whether this was a full tank.", "full_tank")
    elif kind in ("repair", "maintenance", "service"):
        cost = cost_field(data.get("cost"), "cost")
        if kind == "service":
            schedule_id = old["schedule_id"] if old else identifier(data.get("schedule_id"), "schedule_id")
            if not old and schedule_id is None:
                raise ApiError("Choose a maintenance schedule.", "schedule_id")
            if schedule_id is not None:
                schedule = owned_row(conn, "car_schedules", car_id, schedule_id)
                name = old["name"] if old else schedule["name"]
        if not name:
            name = line(data.get("name"), "name", 80)
    values = (day, odometer, name, note, cost, fuel_ml, full_tank, schedule_id)
    if record_id is None:
        return conn.execute("""
            INSERT INTO car_activity (car_id, kind, recorded_on, odometer_km, name, note,
                                      cost, fuel_ml, full_tank, schedule_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (car_id, kind, *values)).lastrowid
    conn.execute("""
        UPDATE car_activity SET recorded_on = ?, odometer_km = ?, name = ?, note = ?,
            cost = ?, fuel_ml = ?, full_tank = ?, schedule_id = ? WHERE id = ?
    """, (*values, record_id))
    return record_id


def import_odometer(conn, car_id, data, today):
    raw = data.get("csv")
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError("Choose a UTF-8 CSV file with date and odometer_km columns.", "csv")
    source = io.StringIO(raw.removeprefix("\ufeff"), newline="")
    delimiter = ";" if ";" in source.readline() else ","
    source.seek(0)
    reader = csv.reader(source, delimiter=delimiter, strict=True)
    incoming = set()
    try:
        header = next(reader, [])
        if [name.strip().lower() for name in header] != ["date", "odometer_km"]:
            raise ApiError("Use the columns date and odometer_km, in that order.", "csv")
        for index, row in enumerate(reader):
            if index >= MAX_CSV_ROWS:
                raise ApiError(f"Import up to {MAX_CSV_ROWS:,} rows at once.", "csv")
            if not row or all(not item.strip() for item in row):
                continue
            try:
                if len(row) != 2:
                    raise ApiError("Use two columns per row.")
                day = recorded_date(row[0].strip(), "date", today)
                mileage = kilometres(row[1].strip(), "odometer_km")
            except ApiError as error:
                raise ApiError(f"CSV line {reader.line_num}. {error}", "csv") from error
            incoming.add((day, mileage))
    except csv.Error as error:
        raise ApiError(f"CSV line {reader.line_num} could not be read.", "csv") from error
    if not incoming:
        raise ApiError("Add at least one odometer reading to the CSV.", "csv")
    existing = {tuple(row) for row in conn.execute("""
        SELECT DISTINCT recorded_on, odometer_km FROM car_activity WHERE car_id = ?
    """, (car_id,))}
    additions = incoming - existing
    earlier_max = 0
    most_recent_day = None
    latest_km = 0
    for day, mileage in sorted(existing | incoming):
        if day != most_recent_day:
            earlier_max = latest_km
            most_recent_day = day
        if mileage < earlier_max:
            raise ApiError(f"The readings around {day} conflict with another dated odometer reading.", "csv")
        latest_km = mileage
    conn.executemany("""
        INSERT INTO car_activity (car_id, kind, recorded_on, odometer_km) VALUES (?, 'odometer', ?, ?)
    """, ((car_id, day, mileage) for day, mileage in sorted(additions)))
    return {"imported": len(additions), "duplicates": len(incoming & existing)}


def fuel_report(activity):
    total_ml = total_cost = measured_ml = distance = pending_ml = 0
    full_at = None
    for row in activity:
        if row["kind"] != "fuel":
            continue
        volume = row["fuel_ml"]
        total_ml += volume
        total_cost += row["cost"]
        if full_at is not None:
            pending_ml += volume
        if not row["full_tank"]:
            continue
        if full_at is not None and row["odometer_km"] == full_at:
            continue
        if full_at is not None:
            distance += row["odometer_km"] - full_at
            measured_ml += pending_ml
        full_at = row["odometer_km"]
        pending_ml = 0
    consumption = Fraction(measured_ml, distance * 10) if distance else None
    price = Fraction(total_cost * 1000, total_ml) if total_ml else None
    cost_per_km = consumption * price / 100 if consumption is not None else None
    return {
        "cost": cents(total_cost), "litres": total_ml / 1000,
        "consumption": round(float(consumption), 2) if consumption is not None else None,
        "price_per_litre": cents(round(price)) if price is not None else None,
        "cost_per_km": cents(round(cost_per_km)) if cost_per_km is not None else None,
        "measured_km": distance,
    }, cost_per_km


def driving_rate(daily):
    if len(daily) < 2:
        return None, None
    latest_day = max(daily)
    cutoff = date.fromordinal(max(1, latest_day.toordinal() - DRIVING_LOOKBACK))
    baseline = max((day for day in daily if day <= cutoff), default=min(daily))
    rate = Fraction(daily[latest_day] - daily[baseline], (latest_day - baseline).days)
    if not rate:
        return None, None
    return rate, {
        "from": baseline.isoformat(), "through": latest_day.isoformat(),
        "km_in_year": round(rate * FORECAST_DAYS),
    }


def service_report(schedules, activity, latest, rate, today):
    completed = defaultdict(list)
    for event in activity:
        if event["schedule_id"] is not None:
            completed[event["schedule_id"]].append(event)
    reading_date = date.fromisoformat(latest["recorded_on"]) if latest else None
    if rate:
        projected_today = latest["odometer_km"] + rate * (today - reading_date).days
        projected_year_end = projected_today + rate * FORECAST_DAYS
    result = []
    for schedule in schedules:
        history = completed[schedule["id"]]
        last = history[-1] if history else None
        anchor = last["odometer_km"] if last else schedule["baseline_km"]
        interval = schedule["interval_km"]
        due_km = anchor + interval
        remaining = due_km - latest["odometer_km"] if latest else None
        estimate = schedule["estimate"]
        if estimate is None:
            estimate = next((event["cost"] for event in reversed(history) if event["cost"] is not None), None)
        due_on = None
        count = 0 if remaining is not None and remaining > 0 and rate else None
        if remaining is not None and remaining <= 0:
            due_on = today.isoformat()
            count = 1
        elif rate:
            days = ceil(Fraction(remaining) / rate)
            if days <= (date.max - reading_date).days:
                due_on = (reading_date + timedelta(days=days)).isoformat()
        if rate:
            # Budget one service if due now, followed by a fresh interval from estimated current mileage.
            first_in_forecast = max(due_km, projected_today)
            count = (1 + int((projected_year_end - first_in_forecast) // interval)
                     if first_in_forecast <= projected_year_end else 0)
        result.append(dict(
            schedule, estimate=cents(schedule["estimate"]), projected_cost=cents(estimate),
            last_service_on=last["recorded_on"] if last else None, last_service_km=anchor,
            due_km=due_km, remaining_km=remaining, due_on=due_on, count_in_year=count,
        ))
    return sorted(result, key=lambda row: (row["due_km"], row["id"]))


def car_report(conn, car, today, *, include_history=True):
    car_id = car["id"]
    activity = [dict(row) for row in conn.execute("""
        SELECT * FROM car_activity WHERE car_id = ? ORDER BY recorded_on, odometer_km, id
    """, (car_id,))]
    schedules = [dict(row) for row in conn.execute("SELECT * FROM car_schedules WHERE car_id = ?", (car_id,))]
    saved = {row["part"]: row for row in conn.execute("SELECT * FROM car_inspections WHERE car_id = ?", (car_id,))}
    parts = []
    for key, part in equipped_parts(car).items():
        record = saved.get(key)
        parts.append(dict(
            part, condition=record["condition"] if record else "unchecked",
            estimate=cents(record["estimate"]) if record else None,
            note=record["note"] if record else "",
        ))

    daily = {date.fromisoformat(row["recorded_on"]): row["odometer_km"] for row in activity}
    latest = activity[-1] if activity else None
    rate, driving = driving_rate(daily)
    fuel, fuel_cost_per_km = fuel_report(activity)
    services = service_report(schedules, activity, latest, rate, today)
    paid = dict.fromkeys(ACTIVITY_KINDS[1:], 0)
    for row in activity:
        if row["cost"] is not None:
            paid[row["kind"]] += row["cost"]
    flagged = [part for part in parts if part["condition"] in ("watch", "repair")]
    scheduled = [item for item in services if item["count_in_year"]]
    scheduled_costs = [int(item["projected_cost"]) * item["count_in_year"] for item in scheduled
                       if item["projected_cost"] is not None]
    repair_costs = [int(part["estimate"]) for part in flagged if part["estimate"] is not None]
    report = {
        "car": car, "today": today.isoformat(),
        "odometer": {key: latest[key] for key in ("recorded_on", "odometer_km")} if latest else None,
        "driving": driving, "fuel": fuel, "schedules": services, "parts": parts,
        "paid": {key: cents(value) for key, value in paid.items()},
        "projection": {
            "fuel": cents(round(rate * FORECAST_DAYS * fuel_cost_per_km))
            if rate is not None and fuel_cost_per_km is not None else None,
            "scheduled": cents(sum(scheduled_costs)) if scheduled_costs else
            ("0" if rate is not None and not scheduled else None),
            "unpriced_services": sum(item["count_in_year"] for item in scheduled if item["projected_cost"] is None),
            "undated_schedules": sum(item["count_in_year"] is None for item in services),
            "health_estimates": cents(sum(repair_costs)) if repair_costs else ("0" if not flagged else None),
            "unpriced_parts": len(flagged) - len(repair_costs),
        },
    }
    if include_history:
        report["activity"] = [dict(row, cost=cents(row["cost"]), full_tank=bool(row["full_tank"]))
                              for row in reversed(activity)]
    return report


def sum_known_costs(amounts):
    prices = [int(amount) for amount in amounts if amount is not None]
    return cents(sum(prices)) if prices else None


def group_cost_report(conn, cars, today):
    rows = []
    upcoming = []
    coverage = dict(missing_fuel=0, undated_schedules=0, unpriced_services=0, unpriced_parts=0)
    for car in cars:
        report = car_report(conn, car, today, include_history=False)
        booked = report["paid"]
        actual = {
            "fuel": booked["fuel"], "maintenance": cents(int(booked["maintenance"]) + int(booked["service"])),
            "repair": booked["repair"], "total": cents(sum(map(int, booked.values()))),
        }
        projection = report["projection"]
        scheduled = projection["scheduled"] if report["schedules"] else "0"
        forecast = {
            "fuel": projection["fuel"], "scheduled": scheduled,
            "planned": sum_known_costs([projection["fuel"], scheduled]),
            "health_estimates": projection["health_estimates"],
        }
        coverage["missing_fuel"] += projection["fuel"] is None
        for key in ("undated_schedules", "unpriced_services", "unpriced_parts"):
            coverage[key] += projection[key]
        rows.append({"car_id": car["id"], "actual": actual, "forecast": forecast})
        for service in report["schedules"]:
            if service["count_in_year"] != 0:
                upcoming.append(dict(car_id=car["id"], **{
                    key: service[key] for key in (
                        "name", "due_on", "due_km", "remaining_km", "count_in_year", "projected_cost",
                    )
                }))

    upcoming.sort(key=lambda row: (row["due_on"] is None, row["due_on"], row["car_id"], row["due_km"]))
    return {
        "cars": rows, "upcoming": upcoming, "coverage": coverage,
        "actual": {key: sum_known_costs(row["actual"][key] for row in rows)
                   for key in ("fuel", "maintenance", "repair", "total")},
        "forecast": {key: sum_known_costs(row["forecast"][key] for row in rows)
                     for key in ("fuel", "scheduled", "planned", "health_estimates")},
    }


def save_part(conn, car, key, data):
    if key not in equipped_parts(car):
        raise ApiError("This part is unavailable for the selected car configuration.", status=404)
    condition = choice(data.get("condition"), CONDITIONS, "condition")
    estimate = cost_field(data.get("estimate"), "estimate")
    note = line(data.get("note", ""), "note", 500, required=False)
    conn.execute("""
        INSERT INTO car_inspections (car_id, part, condition, estimate, note) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (car_id, part) DO UPDATE SET condition = excluded.condition,
            estimate = excluded.estimate, note = excluded.note
    """, (car["id"], key, condition, estimate, note))


def sharing_context(handler, registry, conn):
    active = handler.workspace
    if registry is None:
        current = {"id": active["id"], "name": "Personal", "kind": "profile"}
        return {current["id"]: current}, {current["id"]}, {current["id"]}

    user = handler.user
    if active.get("demo_role"):
        workspaces = [item for item in registry.list(conn)
                      if item.get("demo_owner") == active["demo_owner"] and item["demo_ready"]]
        people = {item["id"] for item in workspaces if item["kind"] == "profile"}
        return {item["id"]: item for item in workspaces}, people, {item["id"] for item in workspaces}

    directory = {item["id"]: item for item in registry.list(conn) if not item.get("demo_role")}
    own = user["profile_id"] if user else active["id"]
    profile = directory[own]
    groups = {group["id"] for group in profile["groups"]}
    people = {member["id"] for group in groups for member in directory[group]["members"]}
    if user and user["is_admin"]:
        people.update(key for key, item in directory.items() if item["kind"] == "profile")
    return directory, {own}, people | groups | {own, active["id"]}


def assignment_rows(directory, workspace_ids):
    return [{"id": workspace_id, "name": directory[workspace_id]["name"], "kind": directory[workspace_id]["kind"]}
            for workspace_id in sorted(workspace_ids)]


def sharing_payload(car, directory, people, workspace_ids):
    assignments = assignment_rows(directory, workspace_ids)
    owners = [item for item in assignments if item["kind"] == "profile"]
    can_manage = not owners or any(item["id"] in people for item in owners)
    return dict(car, owners=owners, can_manage=can_manage, assignments=assignments if can_manage else [])


def save_assignments(conn, car_id, values, eligible):
    if not isinstance(values, list) or not values:
        raise ApiError("Assign the car to at least one person or group.", "workspace_ids")
    workspace_ids = {identifier(key, "workspace_ids") for key in values}
    if not workspace_ids.issubset(eligible):
        raise ApiError("Choose from the listed people and group workspaces.", "workspace_ids")
    conn.execute("DELETE FROM car_assignments WHERE car_id = ?", (car_id,))
    conn.executemany("INSERT INTO car_assignments VALUES (?, ?)", ((car_id, key) for key in sorted(workspace_ids)))


def garage_report(conn, workspace_id, directory, people, eligible, today):
    assignments = defaultdict(set)
    for row in conn.execute("""
        SELECT a.car_id, a.workspace_id FROM car_assignments AS a
        JOIN car_assignments AS scope ON scope.car_id = a.car_id WHERE scope.workspace_id = ?
    """, (workspace_id,)):
        assignments[row["car_id"]].add(row["workspace_id"])
    cars = [sharing_payload(car_payload(row), directory, people, assignments[row["id"]])
            for row in conn.execute("""
                SELECT cars.* FROM cars JOIN car_assignments ON car_assignments.car_id = cars.id
                WHERE car_assignments.workspace_id = ? ORDER BY cars.id
            """, (workspace_id,))]
    return {
        "cars": cars, "drivetrains": DRIVETRAINS, "views": VIEWS,
        "equipment": {key: name for key, name, *_ in PARTS if key in EQUIPMENT},
        "workspace_id": workspace_id, "assignment_options": assignment_rows(directory, eligible),
        "group_costs": group_cost_report(conn, cars, today) if directory[workspace_id]["kind"] == "group" else None,
    }


def handle_cars(handler, registry, path, method, today):
    match = ROUTE.fullmatch(path)
    if not match:
        return None
    car_id, collection, key = match.groups()
    car_id = int(car_id) if car_id else None
    workspace_id = handler.workspace["id"]
    if method == "GET" and collection is not None:
        raise ApiError("This car action is unavailable.", status=405)
    data = {}
    if method in ("POST", "PUT"):
        data = handler.read_data(240_000 if collection == "odometer-import" else 12_000)
    connection = (registry.connection(write=method != "GET") if registry else
                  handler.server.database.connection(write=method != "GET", snapshot=method == "GET"))
    with connection as conn:
        directory, people, eligible = sharing_context(handler, registry, conn)
        if car_id is None:
            if method == "GET":
                return garage_report(conn, workspace_id, directory, people, eligible, today), 200
            if method == "POST":
                created = save_car(conn, data)
                save_assignments(conn, created, data.get("workspace_ids", [workspace_id]), eligible)
                return {"id": created}, 201
            raise ApiError("This car action is unavailable.", status=405)

        assigned = {row[0] for row in conn.execute("SELECT workspace_id FROM car_assignments WHERE car_id = ?", (car_id,))}
        if workspace_id not in assigned:
            raise ApiError("This car is unavailable in this workspace.", status=404)
        car = sharing_payload(car_payload(row_by_id(conn, "cars", car_id)), directory, people, assigned)
        if collection is None:
            if method == "GET":
                return car_report(conn, car, today), 200
            if method == "PUT":
                if "workspace_ids" in data:
                    if not car["can_manage"]:
                        raise ApiError("Only the assigned people can change this car’s sharing.", "workspace_ids", 403)
                    save_assignments(conn, car_id, data["workspace_ids"], eligible | assigned)
                return {"id": save_car(conn, data, car_id)}, 200
            if method == "DELETE":
                if not car["can_manage"]:
                    raise ApiError("Only the assigned people can delete this car.", status=403)
                conn.execute("DELETE FROM cars WHERE id = ?", (car_id,))
                return {"id": car_id}, 200
        if collection == "odometer-import" and method == "POST" and key is None:
            return import_odometer(conn, car_id, data, today), 200
        if collection == "parts" and method == "PUT" and key is not None:
            save_part(conn, car, key, data)
            return {"id": key}, 200
        if collection in ("activities", "schedules"):
            table = "car_activity" if collection == "activities" else "car_schedules"
            record_id = identifier(key, "id")
            if (method == "POST" and record_id is None) or (method == "PUT" and record_id is not None):
                saved = (save_activity(conn, car_id, data, today, record_id) if collection == "activities"
                         else save_schedule(conn, car_id, data, record_id))
                return {"id": saved}, 201 if method == "POST" else 200
            if method == "DELETE" and record_id is not None:
                owned_row(conn, table, car_id, record_id)
                conn.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
                return {"id": record_id}, 200
    raise ApiError("This car action is unavailable.", status=405)
