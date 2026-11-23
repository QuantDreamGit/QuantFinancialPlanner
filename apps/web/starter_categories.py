"""The optional category tree for personal profiles and group ledgers."""

from .values import ApiError


STARTER_CATEGORIES = (
    ("income", "Employment", None, "mint"),
    ("income", "Salary", "Employment", "mint"),
    ("income", "Bonus", "Employment", "mint"),
    ("income", "Freelance", None, "sky"),
    ("income", "Savings interest", None, "mint"),
    ("income", "Other income", None, "slate"),
    ("expense", "Home", None, "rose"),
    ("expense", "Rent", "Home", "rose"),
    ("expense", "Utilities", "Home", "amber"),
    ("expense", "Maintenance", "Home", "slate"),
    ("expense", "Everyday", None, "amber"),
    ("expense", "Groceries", "Everyday", "sky"),
    ("expense", "Dining", "Everyday", "rose"),
    ("expense", "Subscriptions", "Everyday", "violet"),
    ("expense", "Travel & transport", None, "sky"),
    ("expense", "Health", None, "mint"),
    ("expense", "Insurance", None, "slate"),
    ("expense", "Shopping", None, "indigo"),
    ("expense", "Entertainment", None, "violet"),
    ("investment", "Portfolio", None, "violet"),
    ("investment", "Index funds", "Portfolio", "indigo"),
    ("investment", "Bonds", "Portfolio", "sky"),
    ("investment", "Retirement", None, "violet"),
)


def starter_requested(data):
    requested = data.get("starter_categories", False)
    if type(requested) is not bool:
        raise ApiError("Choose whether to add starter categories.", "starter_categories")
    return requested


def apply_starter_categories(database):
    """Merge missing local branches while retaining existing IDs, names, and colors."""
    added = 0
    category_ids = {}
    with database.connection(write=True) as conn:
        for kind, name, parent, color in STARTER_CATEGORIES:
            parent_id = category_ids[kind, parent] if parent else None
            existing = conn.execute("""
                SELECT id FROM categories WHERE id > 0 AND kind = ? AND parent_id IS ? AND name = ?
            """, (kind, parent_id, name)).fetchone()
            if existing is None:
                category_id = conn.execute("""
                    INSERT INTO categories (name, kind, parent_id, color) VALUES (?, ?, ?, ?)
                """, (name, kind, parent_id, color)).lastrowid
                added += 1
            else:
                category_id = existing["id"]
            category_ids[kind, name] = category_id
    return {"added": added}


def handle_starter_categories(handler, path, query, method, today):
    if path != "/api/categories/starter":
        return None
    if method == "GET":
        return {"categories": [
            {"kind": kind, "name": name, "parent": parent, "color": color}
            for kind, name, parent, color in STARTER_CATEGORIES
        ]}, 200
    if method == "POST":
        handler.read_data(1024)
        return apply_starter_categories(handler.database), 200
    raise ApiError("This starter category action is unavailable.", status=405)
