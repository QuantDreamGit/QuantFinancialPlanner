import json
import re
import subprocess
from datetime import date

from .values import ApiError, euros, money


FIELDS = ("startingMonth", "startingCash", "monthlyNetCashFlow", "minimumReserve", "purchasePrice", "dueMonth")
PLANNING_ISSUES = {
    "invalid_month": ("Choose valid calendar months for the start and purchase.", None),
    "expense_before_snapshot": ("Choose a due month on or after the starting month.", "dueMonth"),
    "non_positive_expense": ("Enter a purchase price above €0.", "purchasePrice"),
    "negative_reserve": ("Enter a minimum reserve of €0 or more.", "minimumReserve"),
    "arithmetic_overflow": ("The projection exceeds the supported range of euro amounts.", None),
}


def calendar_month(value, field):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        try:
            date.fromisoformat(f"{value}-01")
            return value
        except ValueError:
            pass
    raise ApiError("Enter a calendar month in YYYY-MM format.", field)


class Projection:
    def __init__(self, binary):
        self.binary = binary

    def run(self, *arguments):
        try:
            completed = subprocess.run(
                [str(self.binary), *arguments], stdin=subprocess.DEVNULL, capture_output=True,
                check=True, timeout=10,
            )
            return json.loads(completed.stdout)
        except FileNotFoundError as error:
            raise ApiError("Build the C++ projection engine to compare purchase plans. See the project README.", status=503) from error
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ApiError("The projection engine could not complete the comparison.", status=503) from error

    def example(self):
        return self.run("--example")

    def compare(self, data):
        start, *amount_fields, due = FIELDS
        arguments = [
            calendar_month(data.get(start), start),
            *(money(data.get(key), key) for key in amount_fields),
            calendar_month(data.get(due), due),
        ]
        result = self.run(*(str(value) for value in arguments))
        if "error" in result:
            message, field = PLANNING_ISSUES.get(result["error"], ("Planning failed.", None))
            raise ApiError(message, field)
        inputs = {key: euros(value) if isinstance(value, int) else value for key, value in zip(FIELDS, arguments)}
        return inputs, result
