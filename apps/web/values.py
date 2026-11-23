import re
from datetime import date


class ApiError(Exception):
    def __init__(self, message, field=None, status=422):
        super().__init__(message)
        self.field = field
        self.status = status


def money(value, field):
    text = value.strip() if isinstance(value, str) else ""
    match = re.fullmatch(r"([+-]?)0*([0-9]{1,19})(?:[.,]([0-9]{1,2}))?", text)
    if match:
        sign, whole, fraction = match.groups()
        cents = int(whole) * 100 + int((fraction or "").ljust(2, "0"))
        if sign == "-":
            cents = -cents
        if -(2**63) <= cents < 2**63:
            return cents
    raise ApiError("Enter a euro amount with up to two decimal places.", field)


def euros(cents):
    whole, fraction = divmod(abs(cents), 100)
    sign = "-" if cents < 0 else ""
    return f"{sign}{whole}.{fraction:02d}"


def recorded_date(value, field, today):
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value and day <= today:
            return value
    except (TypeError, ValueError):
        pass
    raise ApiError("Enter today’s date or an earlier calendar date.", field)


def line(value, field, limit, *, required=True):
    if isinstance(value, str):
        value = value.strip()
        if len(value) <= limit and (value or not required) and not re.search(r"[\x00\ud800-\udfff]", value):
            return value
    start = "Enter" if required else "Use"
    raise ApiError(f"{start} up to {limit} characters.", field)


def choice(value, options, field):
    if not isinstance(value, str) or value not in options:
        raise ApiError("Choose one of the listed options.", field)
    return value


def identifier(value, field):
    if value in ("", None):
        return None
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,18}", value):
        value = int(value)
    if type(value) is int and (0 < value < 2**63 or -(2**53 - 1) <= value <= -1_000_000_000):
        return value
    raise ApiError("Choose an existing item.", field)
