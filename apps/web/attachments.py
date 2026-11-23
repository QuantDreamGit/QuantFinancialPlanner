"""Transaction documents stored as private, non-rendered ledger attachments."""
import base64
import binascii
import hashlib
import re
import unicodedata
from dataclasses import dataclass

from .values import ApiError


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BASE64_BYTES = 4 * ((MAX_FILE_BYTES + 2) // 3)
MAX_UPLOAD_BODY = MAX_BASE64_BYTES + 4096
MAX_ATTACHMENTS = 12
ROUTE = re.compile(r"/api/transactions/(-?[1-9][0-9]{0,17})/attachments(?:/([1-9][0-9]{0,17}))?")
ZIP_DOCUMENTS = {"docx", "xlsx", "pptx", "odt", "ods", "odp"}
LEGACY_OFFICE = {"doc", "xls", "ppt"}
TEXT = {"txt", "csv"}
ISO_IMAGES = {"heic", "heif", "avif"}
SIGNATURES = {
    "png": (bytes.fromhex("89 50 4e 47 0d 0a 1a 0a"),),
    "jpg": (bytes.fromhex("ff d8 ff"),),
    "jpeg": (bytes.fromhex("ff d8 ff"),),
    "gif": (b"GIF87a", b"GIF89a"),
    "bmp": (b"BM",),
    "tif": (bytes.fromhex("49 49 2a 00"), bytes.fromhex("4d 4d 00 2a")),
    "rtf": (rb"{\rtf",),
}
SIGNATURES["tiff"] = SIGNATURES["tif"]
EXTENSIONS = frozenset(SIGNATURES) | ZIP_DOCUMENTS | LEGACY_OFFICE | TEXT | ISO_IMAGES | {"pdf", "webp"}
OFFICE_HEADER = bytes.fromhex("d0 cf 11 e0 a1 b1 1a e1")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS entry_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE CHECK (entry_id > 0),
    name TEXT NOT NULL,
    content BLOB NOT NULL CHECK (typeof(content) = 'blob' AND length(content) BETWEEN 1 AND {MAX_FILE_BYTES}),
    digest BLOB NOT NULL,
    uploaded_on TEXT NOT NULL,
    UNIQUE (entry_id, name, digest)
);
"""


@dataclass(frozen=True)
class Download:
    body: bytes
    name: str


def initialize_attachments(database):
    with database.connection() as conn:
        conn.execute(SCHEMA)


def _entry_exists(conn, entry_id):
    if entry_id <= 0 or not conn.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,)).fetchone():
        raise ApiError("This saved transaction was not found.", status=404)


def _filename(raw):
    if not isinstance(raw, str) or any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in raw):
        raise ApiError("Choose a file with a plain filename.", "name")
    name = unicodedata.normalize("NFC", raw.strip())
    if not name or len(name) > 180 or "/" in name or "\\" in name:
        raise ApiError("Use a filename of up to 180 characters without path separators.", "name")
    stem, _, extension = name.rpartition(".")
    extension = extension.lower()
    if not stem or extension not in EXTENSIONS:
        raise ApiError("Choose a PDF, image, text, spreadsheet, or office document.", "name")
    return name, extension


def _file_content(raw):
    if not isinstance(raw, str) or not raw:
        raise ApiError("Choose a file to attach.", "file")
    if len(raw) > MAX_BASE64_BYTES:
        raise ApiError("Attach a file up to 10 MiB.", "file", 413)
    try:
        content = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError("The attachment encoding is invalid. Choose the file again.", "file") from None
    if not content:
        raise ApiError("Choose a file with content.", "file")
    if len(content) > MAX_FILE_BYTES:
        raise ApiError("Attach a file up to 10 MiB.", "file", 413)
    return content


def _matches_type(content, extension):
    if extension in SIGNATURES:
        return content.startswith(SIGNATURES[extension])
    if extension == "pdf":
        return b"%PDF-" in content[:1024]
    if extension == "webp":
        return content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    if extension in ISO_IMAGES:
        return len(content) >= 12 and content[4:8] == b"ftyp"
    if extension in ZIP_DOCUMENTS:
        return content.startswith((b"PK\x03\x04", OFFICE_HEADER))
    if extension in LEGACY_OFFICE:
        return content.startswith(OFFICE_HEADER) or (
            extension == "doc" and content.startswith(SIGNATURES["rtf"])
        )
    return content.startswith((bytes.fromhex("ff fe"), bytes.fromhex("fe ff"))) or 0 not in content


def list_attachments(database, entry_id):
    with database.connection(snapshot=True) as conn:
        _entry_exists(conn, entry_id)
        return [dict(row) for row in conn.execute("""
            SELECT id,name,length(content) AS size,uploaded_on FROM entry_attachments
            WHERE entry_id = ? ORDER BY id
        """, (entry_id,))]


def save_attachment(database, entry_id, data, today):
    name, extension = _filename(data.get("name"))
    content = _file_content(data.get("file"))
    if not _matches_type(content, extension):
        raise ApiError("The attachment does not match its file extension.", "file")
    digest = hashlib.sha256(content).digest()
    uploaded = today.isoformat()
    with database.connection(write=True) as conn:
        _entry_exists(conn, entry_id)
        existing = conn.execute("""
            SELECT id,name,length(content) AS size,uploaded_on FROM entry_attachments
            WHERE entry_id=? AND name=? AND digest=?
        """, (entry_id, name, digest)).fetchone()
        if existing:
            return dict(existing), 200
        if conn.execute("SELECT COUNT(*) FROM entry_attachments WHERE entry_id=?", (entry_id,)).fetchone()[0] >= MAX_ATTACHMENTS:
            raise ApiError(f"A transaction can have up to {MAX_ATTACHMENTS} attachments.", "file", 409)
        item_id = conn.execute("""
            INSERT INTO entry_attachments(entry_id,name,content,digest,uploaded_on)
            VALUES(?,?,?,?,?)
        """, (entry_id, name, content, digest, uploaded)).lastrowid
    return {"id": item_id, "name": name, "size": len(content), "uploaded_on": uploaded}, 201


def download_attachment(database, entry_id, attachment_id):
    with database.connection(snapshot=True) as conn:
        _entry_exists(conn, entry_id)
        row = conn.execute("SELECT name,content FROM entry_attachments WHERE id=? AND entry_id=?",
                           (attachment_id, entry_id)).fetchone()
    if row is None:
        raise ApiError("This attachment was not found.", status=404)
    return Download(body=row["content"], name=row["name"])


def delete_attachment(database, entry_id, attachment_id):
    with database.connection(write=True) as conn:
        _entry_exists(conn, entry_id)
        result = conn.execute("DELETE FROM entry_attachments WHERE id=? AND entry_id=?",
                              (attachment_id, entry_id))
        if not result.rowcount:
            raise ApiError("This attachment was not found.", status=404)


def handle_attachments(handler, path, query, method, today):
    match = ROUTE.fullmatch(path)
    if match is None:
        return None
    entry_id = int(match[1])
    if entry_id <= 0:
        raise ApiError("This saved transaction was not found.", status=404)
    database = handler.database
    if match[2] is None:
        if method == "GET":
            return {"attachments": list_attachments(database, entry_id)}, 200
        if method == "POST":
            return save_attachment(database, entry_id, handler.read_data(MAX_UPLOAD_BODY), today)
    else:
        attachment_id = int(match[2])
        if method == "GET":
            return download_attachment(database, entry_id, attachment_id), 200
        if method == "DELETE":
            delete_attachment(database, entry_id, attachment_id)
            return {"id": attachment_id}, 200
    raise ApiError("This attachment action is unavailable.", status=405)
