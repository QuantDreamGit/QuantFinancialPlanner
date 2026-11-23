"""Private originals for PDF statement imports in the active workspace."""

import hashlib
import re
import unicodedata

from .attachments import Download
from .storage import row_by_id
from .values import ApiError, identifier, line


MAX_STATEMENT_PDF_BYTES = 12 * 1024 * 1024
MAX_FILENAME_LENGTH = 180
ROUTE = re.compile(r"/api/imports/documents(?:/([1-9][0-9]{0,17}))?")
METADATA_COLUMNS = "id, account_id, filename, name, length(content) AS size, archived_on"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS statement_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT CHECK (account_id > 0),
    filename TEXT NOT NULL,
    name TEXT NOT NULL,
    content BLOB NOT NULL CHECK (typeof(content) = 'blob' AND length(content) BETWEEN 1 AND {MAX_STATEMENT_PDF_BYTES}),
    digest BLOB NOT NULL CHECK (typeof(digest) = 'blob' AND length(digest) = 32),
    archived_on TEXT NOT NULL,
    UNIQUE (account_id, digest)
);
CREATE INDEX IF NOT EXISTS statement_documents_account_order ON statement_documents(account_id, id DESC);
"""


def initialize_statement_documents(database):
    with database.connection() as conn:
        conn.executescript(SCHEMA)


def _clean_filename(raw):
    if not isinstance(raw, str):
        return "Statement.pdf"
    basename = raw.replace("\\", "/").rsplit("/", 1)[-1]
    printable = "".join(char for char in basename if unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
    name = unicodedata.normalize("NFC", printable).strip().rstrip(" .")
    if name.lower().endswith(".pdf"):
        stem, extension = name[:-4], name[-4:]
    else:
        stem, extension = name, ".pdf"
    stem = stem[:MAX_FILENAME_LENGTH - len(extension)].strip(" .")
    return (stem or "Statement") + extension


def _local_account_id(value):
    account_id = identifier(value, "account_id")
    if account_id is None or account_id <= 0:
        raise ApiError("Choose a local checking, savings or cash account for statement imports.", "account_id")
    return account_id


def _metadata(row):
    return dict(row, download_url=f"/api/imports/documents/{row['id']}")


def archive_statement_pdf(database, account_id, filename, name, content, today):
    """Keep the original once per account and preserve the metadata from the first upload."""
    account_id = _local_account_id(account_id)
    filename = _clean_filename(filename)
    name = line(name, "name", 100)
    if not isinstance(content, bytes) or not content.startswith(b"%PDF-"):
        raise ApiError("Choose a PDF document.", "file")
    if len(content) > MAX_STATEMENT_PDF_BYTES:
        raise ApiError("Choose a PDF up to 12 MiB.", "file", 413)
    digest = hashlib.sha256(content).digest()
    with database.connection(write=True) as conn:
        account = row_by_id(conn, "accounts", account_id, "account_id")
        if account["kind"] == "investment":
            raise ApiError("Choose a checking, savings or cash account for statement imports.", "account_id")
        existing = conn.execute(f"""
            SELECT {METADATA_COLUMNS} FROM statement_documents WHERE account_id = ? AND digest = ?
        """, (account_id, digest)).fetchone()
        if existing is not None:
            return _metadata(existing)
        cursor = conn.execute("""
            INSERT INTO statement_documents (account_id, filename, name, content, digest, archived_on)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, filename, name, content, digest, today.isoformat()))
        row = conn.execute(f"SELECT {METADATA_COLUMNS} FROM statement_documents WHERE id = ?",
                           (cursor.lastrowid,)).fetchone()
    return _metadata(row)


def _integer_parameter(query, key, default, maximum):
    raw = query.get(key, str(default))
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,9}", raw):
        raise ApiError(f"Choose a {key} from 1 to {maximum:,}.", key)
    number = int(raw)
    if not 1 <= number <= maximum:
        raise ApiError(f"Choose a {key} from 1 to {maximum:,}.", key)
    return number


def _list_documents(database, query):
    page = _integer_parameter(query, "page", 1, 1_000_000)
    size = _integer_parameter(query, "size", 25, 50)
    account_id = _local_account_id(query["account_id"]) if "account_id" in query else None
    where, args = ("WHERE account_id = ?", (account_id,)) if account_id else ("", ())
    with database.connection(snapshot=True) as conn:
        total = conn.execute(f"SELECT count(*) FROM statement_documents {where}", args).fetchone()[0]
        rows = conn.execute(f"""
            SELECT {METADATA_COLUMNS} FROM statement_documents {where}
            ORDER BY id DESC LIMIT ? OFFSET ?
        """, (*args, size, (page - 1) * size)).fetchall()
    return {"documents": [_metadata(row) for row in rows], "total": total, "page": page, "size": size}


def _download_document(database, item_id):
    with database.connection(snapshot=True) as conn:
        row = conn.execute("SELECT filename, content FROM statement_documents WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ApiError("This original statement was not found.", status=404)
    return Download(body=row["content"], name=row["filename"])


def _delete_document(database, item_id):
    with database.connection(write=True) as conn:
        if not conn.execute("DELETE FROM statement_documents WHERE id = ?", (item_id,)).rowcount:
            raise ApiError("This original statement was not found.", status=404)
    return {"id": item_id}


def handle_statement_documents(handler, path, query, method, today):
    match = ROUTE.fullmatch(path)
    if match is None:
        return None
    database = handler.database
    if match[1] is None:
        if method == "GET":
            return _list_documents(database, query), 200
    else:
        item_id = int(match[1])
        if method == "GET":
            return _download_document(database, item_id), 200
        if method == "DELETE":
            return _delete_document(database, item_id), 200
    raise ApiError("This original statement action is unavailable.", status=405)
