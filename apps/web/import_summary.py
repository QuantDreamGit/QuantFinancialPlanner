"""Summaries of signed imported amounts, using integer cents throughout."""

from collections import Counter, defaultdict


LARGE_EXPENSE_CENTS = 25_000
NON_EXPENSE_KINDS = {"transfer", "investment", "valuation", "settlement"}


def import_report(rows):
    small = defaultdict(int)
    counts = Counter()
    large = []
    credits = other = attention = 0
    for row in rows:
        if row.get("blocked") or row.get("errors") or row.get("review_hold") or row["amount"] is None or not row["posted_on"]:
            attention += 1
            continue
        amount = int(row["amount"])
        if row.get("kind") in NON_EXPENSE_KINDS or row.get("action") in {"transfer", "settle", "expected"}:
            other += amount
        elif amount > 0:
            credits += amount
        elif -amount > LARGE_EXPENSE_CENTS:
            large.append({
                "name": row.get("statement_description") or row["name"],
                "posted_on": row["posted_on"], "amount": str(-amount),
                "reference": row.get("reference", ""),
                "document_id": row.get("document_id"), "page": row.get("page"),
            })
        elif amount < 0:
            month = row["posted_on"][:7]
            small[month] -= amount
            counts[month] += 1
    small_total = sum(small.values())
    large_total = sum(int(item["amount"]) for item in large)
    return {
        "threshold": str(LARGE_EXPENSE_CENTS), "expense_total": str(small_total + large_total),
        "small_total": str(small_total), "large_total": str(large_total),
        "small_groups": [{"month": month, "amount": str(amount), "count": counts[month]}
                         for month, amount in sorted(small.items())],
        "large_expenses": sorted(large, key=lambda item: (item["posted_on"], item["name"])),
        "credits_total": str(credits), "other_net": str(other), "needs_review": attention,
    }
