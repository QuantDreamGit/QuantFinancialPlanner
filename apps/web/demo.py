from datetime import date

from .reports import month_start
from .storage import Ledger


def sample_ledger(today):
    opened_on = month_start(today, -15).isoformat()
    accounts = [
        {"id": 1, "name": "Everyday checking", "kind": "checking", "opening_balance": 532_000, "opened_on": opened_on},
        {"id": 2, "name": "Rainy day savings", "kind": "savings", "opening_balance": 1_120_000, "opened_on": opened_on},
        {"id": 3, "name": "Investment portfolio", "kind": "investment", "opening_balance": 1_547_000, "opened_on": opened_on},
    ]
    categories = [
        {"id": key, "kind": kind, "name": name, "color": color, "parent_id": parent}
        for key, kind, name, color, parent in (
            (1, "income", "Employment", "mint", None),
            (2, "income", "Freelance", "sky", None),
            (4, "expense", "Home", "rose", None),
            (5, "expense", "Rent", "rose", 4),
            (6, "expense", "Utilities", "amber", 4),
            (7, "expense", "Everyday", "amber", None),
            (8, "expense", "Groceries", "sky", 7),
            (9, "expense", "Dining", "rose", 7),
            (10, "expense", "Travel & transport", "sky", None),
            (11, "investment", "Portfolio", "violet", None),
            (12, "investment", "Index funds", "indigo", 11),
            (13, "investment", "Retirement", "violet", None),
            (14, "income", "Savings interest", "mint", None),
        )
    ]
    entries = []

    def record(month, day, kind, name, amount, category=None, labels=(), account=1, destination=None):
        posted_on = date(month.year, month.month, day)
        if posted_on <= today:
            entries.append({
                "id": len(entries) + 1, "kind": kind, "name": name, "amount": amount, "posted_on": posted_on.isoformat(),
                "account_id": account, "destination_id": destination, "category_id": category,
                "labels": list(labels), "note": "",
            })

    returns = [18_200, -37_500, 46_100, 32_800, -14_700, 9_800, 51_400, -41_200]
    for offset in range(-15, 1):
        month = month_start(today, offset)
        n = offset + 15
        record(month, 1, "income", "Monthly salary", 424_000 + max(0, n - 7) * 2_500, 1, ("recurring",))
        record(month, 2, "expense", "Apartment rent", 142_500, 5, ("recurring",))
        record(month, 4, "expense", "Weekly groceries", 12_340 + n * 174, 8, ("home",))
        record(month, 5, "investment", "Index fund contribution", 52_500 + n * 500, 12, ("long-term",), destination=3)
        record(month, 6, "transfer", "Monthly savings", 55_000, destination=2)
        record(month, 7, "expense", "Phone and utilities", 18_000 + (n % 4) * 2_600, 6, ("recurring",))
        record(month, 9, "income", "Freelance project", 29_500 + (n % 4) * 9_250, 2, ("side project",))
        record(month, 11, "expense", "Dinner with friends", 6_430 + n * 218, 9, ("weekend",))
        record(month, 13, "expense", "Transit and travel", 11_350 + (n % 5) * 4_000, 10, ("commute",))
        record(month, 15, "investment", "Retirement contribution", 23_000, 13, ("long-term",), destination=3)
        record(month, 17, "expense", "Local market", 8_750 + n * 111, 8, ("home",))
        record(month, 19, "income", "Savings interest", 2_150 + n * 77, 14, account=2)
        record(month, 22, "expense", "Weekend trip" if n % 4 == 0 else "Travel pass",
               156_000 if n in (7, 12) else 20_500 + n * 625, 10, ("travel",))
        record(month, 25, "valuation", "Portfolio value update", returns[n % len(returns)], account=3)
    entries.sort(key=lambda entry: (entry["posted_on"], entry["id"]))
    return Ledger(accounts, categories, entries)
