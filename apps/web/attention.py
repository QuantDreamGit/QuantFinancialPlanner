from collections import Counter

from .forecast import cash_path
from .reports import DAY
from .shared import _expected_status


def attention(database, today):
    opening, events, scope = cash_path(database, today, today + 30 * DAY)
    weekly = [
        {"date": event["date"].isoformat(), "name": event["name"], "amount": str(event["amount"])}
        for event in events if event["kind"] == "expense" and event["amount"] < 0 and event["date"] < today + 7 * DAY
    ]
    next_return = next(({"date": event["date"].isoformat(), "name": event["name"], "amount": str(event["amount"])}
                        for event in events if event.get("purpose") == "return"), None)
    changes = Counter()
    for event in events:
        changes[event["date"]] += event["amount"]
    balance = opening
    low = None
    for offset in range(31):
        moment = today + offset * DAY
        balance += changes[moment]
        if low is None or balance < low[1]:
            low = (moment, balance)

    def overdue(source):
        with source.connection(snapshot=True) as conn:
            return sum(item["due_on"] < today.isoformat() and item["signed_amount"] > 0
                       for item, _funded in _expected_status(conn))

    repayments = overdue(database)
    overdue_profile = None
    if getattr(database, "kind", None) == "group":
        for member in database.registry.workspace(database.ident)["members"]:
            count = overdue(database.registry.database(dict(member, kind="profile")))
            if count and not repayments:
                overdue_profile = member["id"]
            repayments += count
    return {"bills": weekly[:6], "bill_count": len(weekly), "overdue": repayments, "next_return": next_return,
            "overdue_profile": overdue_profile,
            "low": {"date": low[0].isoformat(), "amount": str(low[1])} if scope else None}
