"""Review and reconcile statement and Amazon order exports."""

import base64
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import shutil
import subprocess
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .forecast import calendar_occurrences, post_occurrence
from .import_ai import configured as ai_configured, extract_statement, validate_api_key
from .import_openai import MODEL as OPENAI_DEFAULT_MODEL, MODELS as OPENAI_MODELS
from .import_openai import configured as openai_configured, extract_statement as extract_openai_statement
from .import_openai import suggest_csv_mapping, suggest_review_edits
from .import_openai import validate_api_key as validate_openai_key, validate_model as validate_openai_model
from .import_summary import import_report
from .reports import movements
from .shared import _expected_status, _record_expected, _settle
from .statement_documents import MAX_STATEMENT_PDF_BYTES, archive_statement_pdf
from .storage import row_by_id
from .values import ApiError, choice, euros, identifier, line, money, recorded_date
from .workspaces import owned_account


MAX_FILE = MAX_STATEMENT_PDF_BYTES
MAX_TEXT = 4 * 1024 * 1024
MAX_RAW_ROWS = 20_000
MAX_ROWS = 4_000
MAX_COMMIT = 24 * 1024 * 1024
MAX_REVIEW_PROMPT = 2 * 1024 * 1024
MAX_CATEGORY_ROWS = 120
MAX_ARCHIVE_FILES = 2_000
DAY = timedelta(days=1)
TOKEN_KEY = secrets.token_bytes(32)
SOURCES = ("csv", "pdf", "amazon")
BANK_SOURCES = {"csv", "pdf"}
GENERIC_PAYMENT_WORDS = {"bank", "card", "credit", "debit", "expense", "mastercard", "online", "payment",
                         "pos", "purchase", "sepa", "transaction", "transfer", "visa"}
DATE_FIELDS = ("auto", "dmy", "mdy", "ymd")
AMOUNT_MODES = ("debit-negative", "debit-positive", "separate")
DEFAULT_IMPORT_CATEGORIES = {"expense": "Other expenses", "income": "Other income"}
COLUMNS = {
    "date": ("date", "postingdate", "bookeddate", "bookingdate", "transactiondate", "valuedate", "completeddate",
             "datum", "boekdatum", "data", "datacontabile", "dataoperazione", "datamovimento", "dataregistrazione"),
    "name": ("name", "description", "payee", "merchant", "counterparty", "details", "remittanceinformation",
             "naamomschrijving", "omschrijving", "descrizione", "descrizioneoperazione", "descrizionemovimento", "causale",
             "operazione"),
    "amount": ("amount", "gross", "bedrag", "bedrageur", "transactionamount", "signedamount", "importo", "importoeur",
               "importomovimento", "importooperazione"),
    "debit": ("debit", "withdrawal", "af", "out", "paidout", "addebito", "addebiti", "uscita", "uscite", "dare"),
    "credit": ("credit", "deposit", "bij", "in", "paidin", "accredito", "accrediti", "entrata", "entrate", "avere"),
    "direction": ("direction", "debitcredit", "creditdebit", "afbij", "bijaf", "dbcr", "segno", "dareavere"),
    "currency": ("currency", "currencycode", "valuta"),
    "reference": ("reference", "transactionid", "transactionreference", "transactieid", "externalid", "idtransazione",
                  "riferimentotransazione"),
}
PREFERRED_CSV_HEADERS = {
    "date": {"postingdate", "bookeddate", "bookingdate", "boekdatum", "datacontabile", "dataregistrazione"},
    "name": {"description", "descrizione", "descrizioneoperazione", "descrizionemovimento"},
}
DATE_TOKEN = r"(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{8})"
AMOUNT_TOKEN = r"(?<!\w)(?:[-+−]\s*)?(?:EUR\s*|€\s*)?(?:[-+−]\s*)?(?:\d{1,3}(?:[., ]\d{3})+|\d+)[.,]\d{2}(?:\s?(?:EUR|€|CR|DR))?(?![\d.,])"
SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    count INTEGER NOT NULL,
    name TEXT
);
CREATE TABLE IF NOT EXISTS import_records (
    account_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    source TEXT NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES import_batches(id),
    entry_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    PRIMARY KEY(account_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS import_record_matches ON import_records(account_id, source, entry_id);
CREATE TABLE IF NOT EXISTS import_details (
    account_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    posted_on TEXT NOT NULL,
    name TEXT NOT NULL,
    amount TEXT NOT NULL,
    reference TEXT NOT NULL,
    PRIMARY KEY(account_id, fingerprint),
    FOREIGN KEY(account_id, fingerprint) REFERENCES import_records(account_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS import_document_rows (
    account_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    document_id INTEGER REFERENCES statement_documents(id) ON DELETE SET NULL,
    page INTEGER,
    evidence TEXT NOT NULL DEFAULT '',
    statement_description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(account_id, fingerprint),
    FOREIGN KEY(account_id, fingerprint) REFERENCES import_records(account_id, fingerprint)
);
"""


def initialize_imports(database):
    with database.connection() as connection:
        connection.executescript(SCHEMA)
    with database.connection(write=True) as connection:
        if not any(row["name"] == "name" for row in connection.execute("PRAGMA table_info(import_batches)")):
            connection.execute("ALTER TABLE import_batches ADD COLUMN name TEXT")


def _normalized_header(value):
    return "".join(char for char in value.casefold() if char.isalnum())


def _scalar(value):
    return str(value) if isinstance(value, (str, int, Decimal)) and type(value) is not bool else ""


def _clean(value, limit):
    return "".join(char for char in " ".join(_scalar(value).split()) if char >= " " and not 0xd800 <= ord(char) <= 0xdfff)[:limit]


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _file(data):
    raw = data.get("file")
    if not isinstance(raw, str) or len(raw) > (MAX_FILE + 2) // 3 * 4:
        raise ApiError("Choose a file up to 12 MB.", "file", 413)
    try:
        binary = base64.b64decode(raw, validate=True)
    except (ValueError, UnicodeError) as error:
        raise ApiError("The uploaded file could not be read.", "file") from error
    if not binary or len(binary) > MAX_FILE:
        raise ApiError("Choose a file up to 12 MB.", "file")
    return binary


def _decode(binary):
    try:
        if binary.startswith((b"\xff\xfe", b"\xfe\xff")):
            value = binary.decode("utf-16")
        else:
            try:
                value = binary.decode("utf-8-sig")
            except UnicodeDecodeError:
                value = binary.decode("cp1252")
    except UnicodeError as error:
        raise ApiError("Save the statement as UTF-8 CSV and try again.", "file") from error
    if "\0" in value:
        raise ApiError("This file does not appear to be a text CSV.", "file")
    return value


def _csv_rows(binary, delimiter="auto"):
    value = _decode(binary)
    if delimiter == "auto":
        first = next((row for row in value.splitlines() if row.strip()), "")
        try:
            delimiter = csv.Sniffer().sniff(first, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    delimiter = {"tab": "\t"}.get(delimiter, delimiter)
    if delimiter not in (",", ";", "\t", "|"):
        raise ApiError("Choose comma, semicolon, tab, or pipe.", "delimiter")
    reader = csv.reader(io.StringIO(value, newline=""), delimiter=delimiter, strict=True)
    try:
        header = next((row for row in reader if any(cell.strip() for cell in row)), [])
        if not 2 <= len(header) <= 100:
            raise ApiError("Select a delimiter that separates the CSV header into columns.", "delimiter")
        headers = [_clean(cell, 100) or "Column " + str(i + 1) for i, cell in enumerate(header)]
        rows = []
        for row in reader:
            if any(cell.strip() for cell in row):
                rows.append((reader.line_num, row))
            if len(rows) > MAX_RAW_ROWS:
                raise ApiError("Split the document into files of up to 20,000 source rows.", "file")
    except csv.Error as error:
        raise ApiError("The CSV has an invalid or oversized field.", "file") from error
    if not rows:
        raise ApiError("The CSV contains no transaction rows.", "file")
    return headers, rows


def _map_columns(headers):
    keys = [_normalized_header(header) for header in headers]
    guess = {}
    for target, aliases in COLUMNS.items():
        matches = [i for i, key in enumerate(keys) if key in aliases]
        if len(matches) > 1:
            preferred = PREFERRED_CSV_HEADERS.get(target, ())
            matches = [index for index in matches if keys[index] in preferred]
        if len(matches) == 1:
            guess[target] = matches[0]
    return guess


def _pick(row, index):
    return row[index].strip() if type(index) is int and 0 <= index < len(row) else ""


def parse_date(value, order):
    source = value.strip()
    iso = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ][0-9][0-9]:[^\r\n]*)?", source)
    if iso:
        year, month, day = map(int, iso.groups())
    elif re.fullmatch(r"\d{8}", source) and order in ("auto", "ymd"):
        year, month, day = int(source[:4]), int(source[4:6]), int(source[6:])
    else:
        parts = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", source)
        if not parts or order == "ymd":
            raise ApiError("Choose a date format that matches the file.", "date")
        first, second, year = map(int, parts.groups())
        if order == "auto":
            if first <= 12 and second <= 12 and first != second:
                raise ApiError("Choose day/month or month/day for this ambiguous date.", "date")
            order = "dmy" if first > 12 else "mdy"
        day, month = (first, second) if order == "dmy" else (second, first)
    try:
        return date(year, month, day)
    except ValueError as error:
        raise ApiError("This date is invalid.", "date") from error


def parse_amount(value):
    """Normalize cents with optional grouping, currency marker, or bank CR/DR."""
    source = value.strip().upper().replace("−", "-").replace("\u00a0", " ").replace("\u202f", " ")
    source = source.replace("EUR", "").replace("€", "").strip()
    sign = 1
    if source.startswith("(") and source.endswith(")"):
        source, sign = source[1:-1].strip(), -1
    ending = re.search(r"(DR|CR|[-+])$", source)
    if ending:
        sign = -1 if ending[0] in ("DR", "-") else 1
        source = source[:ending.start()].strip()
    explicit = re.match(r"^[-+]", source)
    if explicit:
        if ending:
            raise ApiError("This amount has conflicting signs.", "amount")
        sign = -1 if explicit[0] == "-" else 1
        source = source[1:].strip()
    separator = re.search(r"([.,])\d{1,2}$", source)
    if separator:
        whole, decimals = source[:separator.start()], source[separator.start() + 1:]
    else:
        whole, decimals = source, ""
    if re.fullmatch(r"\d+", whole):
        normalized = whole
    else:
        group = re.fullmatch(r"\d{1,3}([., '])\d{3}(?:\1\d{3})*", whole)
        if not group or (separator and group[1] == separator[1]):
            raise ApiError("This amount uses an unrecognized decimal or grouping format.", "amount")
        normalized = whole.replace(group[1], "")
    return sign * money(normalized + ("." + decimals if decimals else ""), "amount")


def _column_amount(row, mapping, mode):
    grab = lambda key: _pick(row, mapping.get(key))
    if mode == "separate":
        debit, credit = (parse_amount(grab(key)) if grab(key) else 0 for key in ("debit", "credit"))
        if debit < 0 or credit < 0 or bool(debit) == bool(credit):
            raise ApiError("Each row needs one positive debit or credit.", "amount")
        return credit - debit
    value = parse_amount(grab("amount"))
    direction = _normalized_header(grab("direction"))
    if direction:
        if direction in ("af", "debit", "d", "dr", "db", "out", "withdrawal", "dare", "addebito", "uscita"):
            return -abs(value)
        if direction in ("bij", "credit", "c", "cr", "in", "deposit", "avere", "accredito", "entrata"):
            return abs(value)
        raise ApiError("This debit/credit label was not recognized.", "direction")
    return value if mode == "debit-negative" else -value


def _pdf_backend():
    if shutil.which("pdftotext"):
        return "poppler"
    if sys.platform == "darwin" and shutil.which("swift"):
        return "pdfkit"
    return None


def _ocr_enabled():
    return sys.platform == "darwin" and shutil.which("swift") is not None


def _pdf_text(binary, ocr=False):
    if not binary.startswith(b"%PDF-"):
        raise ApiError("Choose a PDF document.", "file")
    backend = ("pdfkit" if _ocr_enabled() else None) if ocr else _pdf_backend()
    if backend is None:
        message = ("Local OCR requires macOS and Swift. Run OCR separately, or upload a CSV export." if ocr else
                   "Install Poppler for PDF extraction, or upload a CSV export.")
        raise ApiError(message, "file", 503)
    command = (["pdftotext", "-layout", "-f", "1", "-l", "101", "-", "-"] if backend == "poppler" else
               ["swift", str(Path(__file__).with_name("import_pdf.swift"))])
    if ocr:
        command.append("--ocr")
    try:
        result = subprocess.run(command, input=binary, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=90 if ocr else 45, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        message = ("The scan could not be read. Try an unlocked PDF of up to 20 pages or request a CSV export." if ocr else
                   "The PDF could not be extracted. Try an unlocked, text-based PDF of up to 100 pages or a CSV export.")
        raise ApiError(message, "file") from error
    if len(result.stdout) > MAX_TEXT or result.stdout.count(b"\x0c") > 100:
        raise ApiError("Split the PDF into smaller files or use the CSV export.", "file")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _pdf_rows(contents):
    rows = []
    uncertain = []
    for idx, raw in enumerate(contents.splitlines(), 1):
        match = re.match(rf"^\s*({DATE_TOKEN})\s+(.+)$", raw)
        if not match:
            continue
        first_date, remainder = match.groups()
        if re.search(r"\b(?:balance|saldo|opening|closing|carried forward)\b", remainder, re.I):
            continue
        values = list(re.finditer(AMOUNT_TOKEN, remainder, re.I))
        if not values:
            continue
        trailing = values[-1]
        description = _clean(remainder[:trailing.start()], 100)
        if len(values) > 1 or not description or remainder[trailing.end():].strip():
            uncertain.append(idx)
            continue
        rows.append((idx, [first_date, description, trailing[0], ""]))
    if not rows and not uncertain:
        when = re.search(rf"(?im)^\s*(?:payment date|invoice date|date|datum)\s*:?\s*({DATE_TOKEN})\s*$", contents)
        paid = list(re.finditer(rf"(?im)^\s*(?:total paid|amount paid|totaal betaald)\s*:?\s*({AMOUNT_TOKEN})\s*$", contents))
        if when and len(paid) == 1:
            value = paid[0][1].strip()
            rows.append((1, [when[1], "", value if value.startswith(("-", "−")) else "-" + value, ""]))
    return ["Date", "Description", "Amount", "Currency"], rows, uncertain


def _archive_files(binary):
    if not zipfile.is_zipfile(io.BytesIO(binary)):
        return [("orders", binary)]
    try:
        with zipfile.ZipFile(io.BytesIO(binary)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_FILES:
                raise ApiError("Use the Amazon order-history CSV files directly for this large archive.", "file")
            selected = [entry for entry in infos if not entry.is_dir() and re.search(
                r"(?:^|/)(?:retail[._ -]?order[._ -]?history[^/]*|(?:portability-)?physical[._ -]?orders[^/]*|orders?)\.(?:csv|json)$",
                entry.filename, re.I,
            )]
            if not selected:
                raise ApiError("This archive does not contain a supported Amazon order-history file.", "file")
            if sum(info.file_size for info in selected) > MAX_FILE:
                raise ApiError("Use separate Amazon order-history files of up to 12 MB.", "file", 413)
            return [(entry.filename, archive.read(entry)) for entry in selected]
    except (RuntimeError, OSError, zipfile.BadZipFile, NotImplementedError) as error:
        raise ApiError("This Amazon archive could not be read.", "file") from error


def _amazon_rows(binary):
    def csv_items(raw):
        headers, rows = _csv_rows(raw)
        lookup = {_normalized_header(header): i for i, header in enumerate(headers)}
        if not {"orderid", "orderdate"} <= lookup.keys() or not {"totalowed", "ordertotal", "grandtotal"} & lookup.keys():
            raise ApiError("Choose an Amazon retail order-history export with an order ID, date, and total.", "file")
        for _, row in rows:
            grab = lambda *fields: next((_pick(row, lookup.get(key)) for key in fields if _pick(row, lookup.get(key))), "")
            yield (grab("orderid"), grab("orderdate"), grab("currency", "currencycode"),
                   grab("ordertotal", "grandtotal", "totalowed"), grab("productname", "title"),
                   grab("orderstatus", "shipmentstatus"), bool(grab("ordertotal", "grandtotal")))

    def json_items(raw):
        try:
            records = json.loads(_decode(raw), parse_float=Decimal)
        except (ValueError, RecursionError) as error:
            raise ApiError("This Amazon JSON file could not be read.", "file") from error
        if isinstance(records, dict):
            records = records.get("records")
        if not isinstance(records, list) or len(records) > MAX_RAW_ROWS:
            raise ApiError("Choose a physical order JSON array of up to 20,000 items.", "file")
        for item in records:
            if not isinstance(item, dict) or "orderId" not in item or "orderDate" not in item:
                raise ApiError("Expected physical orders with orderId and orderDate.", "file")
            yield (*[_scalar(item.get(field)) for field in ("orderId", "orderDate", "currencyCode", "totalOwed", "productName", "orderStatus")], True)

    grouped = defaultdict(list)
    total_rows = 0
    for name, raw in _archive_files(binary):
        as_json = name.lower().endswith(".json") or raw.lstrip(b"\xef\xbb\xbf\t\r\n ").startswith((b"[", b"{"))
        for row in (json_items(raw) if as_json else csv_items(raw)):
            total_rows += 1
            if total_rows > MAX_RAW_ROWS:
                raise ApiError("Use separate Amazon exports of up to 20,000 source rows.", "file")
            key = _clean(row[0], 150)
            if key:
                grouped[key].append(row)
    result = []
    for row_number, (order, items) in enumerate(grouped.items(), 1):
        active = [item for item in items if not re.search(r"cancel|declin|pend|unshipped", item[5], re.I)]
        if not active:
            continue
        dates = {item[1][:10] for item in active if item[1]}
        currencies = {item[2].upper() for item in active}
        # The documented physical-orders JSON total is order-level. Privacy CSV line
        # totals may be shipment amounts, so repeated item rows need confirmation.
        totals = {item[3] for item in active if item[3]}
        ambiguous = len(totals) != 1 or len(dates) != 1 or len(currencies) != 1 or (len(active) > 1 and not all(item[6] for item in active))
        products = list(dict.fromkeys(_clean(item[4], 100) for item in active if item[4]))
        description = _clean("Amazon · " + (" / ".join(products) or "Order " + order), 100)
        negative = ""
        if not ambiguous:
            value = next(iter(totals))
            try:
                cents = parse_amount(value)
                if cents > 0:
                    negative = euros(-cents)
                else:
                    ambiguous = True
            except ApiError:
                ambiguous = True
        result.append((row_number, [min(dates, default=""), description, negative, min(currencies, default=""), order], ambiguous))
    if not result:
        raise ApiError("This file contains no supported Amazon orders.", "file")
    return result


def _extract(data):
    source = choice(data.get("source"), SOURCES, "source")
    binary = _file(data)
    if source == "amazon":
        rows = _amazon_rows(binary)
        return source, ["Date", "Description", "Amount", "Currency", "Reference"], [(idx, row) for idx, row, _ in rows], "", [
            idx for idx, _, ambiguous in rows if ambiguous
        ], None
    if source == "pdf":
        contents = _pdf_text(binary, ocr=data.get("ocr") is True)
        headers, rows, messages = _pdf_rows(contents)
        return source, headers, rows, contents, messages, binary
    headers, rows = _csv_rows(binary, data.get("delimiter", "auto"))
    return source, headers, rows, "", [], None


def _pdf_method(data):
    method = choice(data.get("pdf_method", "local"), ("local", "ai", "openai"), "pdf_method")
    if data.get("source") != "pdf" and method != "local":
        raise ApiError("AI extraction is available for PDF files.", "source")
    return method


def _csv_method(data):
    method = choice(data.get("csv_method", "local"), ("local", "openai"), "csv_method")
    if data.get("source") != "csv" and method != "local":
        raise ApiError("Choose a CSV statement for OpenAI column mapping.", "source")
    return method


def _ai_key(data, key_store, user_id, validate):
    supplied = data.get("api_key")
    if supplied is None:
        return (key_store.get(user_id) if key_store else None), False
    return validate(supplied), bool(key_store and key_store.available and not key_store.has(user_id))


def _remember_ai_key(key_store, user_id, api_key, provider, notices):
    try:
        key_store.save(user_id, api_key, only_if_missing=True)
    except ApiError:
        notices.append(f"{provider} worked for this import, but Quant could not save the key. Try the key settings.")


def _ai_pdf(data, database, account_id, key_store, user_id, method, batch_name, today):
    use_openai = method == "openai"
    provider = "OpenAI" if use_openai else "Groq"
    if data.get("ai_consent") is not True:
        material = "the full PDF" if use_openai else "the extracted text"
        raise ApiError(f"Confirm that Quant may send {material} to {provider}.", "ai_consent")
    with database.connection(snapshot=True) as conn:
        _set_account(conn, account_id)
    api_key, remember_key = _ai_key(data, key_store, user_id, validate_openai_key if use_openai else validate_api_key)
    binary = _file(data)
    document = None
    contents = ""
    if use_openai:
        document = archive_statement_pdf(database, account_id, data.get("filename"), batch_name, binary, today)
        extracted = extract_openai_statement(binary, api_key=api_key, model=data["openai_model"])
    else:
        contents = _pdf_text(binary, ocr=data.get("ocr") is True)
        if not contents.strip():
            raise ApiError("No text was found. Enable local OCR for a scan or request a CSV statement.", "file")
        extracted = extract_statement(contents, api_key=api_key)
    notices = list(extracted["notices"])
    if remember_key:
        _remember_ai_key(key_store, user_id, api_key, provider, notices)
    fields = ("date", "description", "amount", "currency", "reference")
    rows = [(index, [row[field] for field in fields]) for index, row in enumerate(extracted["rows"], 1)]
    return ["Date", "Description", "Amount", "Currency", "Reference"], rows, contents, notices, binary, document, extracted


def _batch_name(data, source, today):
    name = line(data.get("name", ""), "name", 100, required=False)
    return name or (f"Bill - {today.isoformat()}" if source == "pdf" else None)


def _workspace_scope(database):
    return _digest(str(database.path.resolve()))


def _mapping(data, headers, source):
    if source != "csv":
        return _map_columns(headers)
    supplied = data.get("mapping")
    if not isinstance(supplied, dict):
        raise ApiError("Map the statement columns before reviewing.", "mapping")
    if any(key not in COLUMNS or type(index) is not int or not 0 <= index < len(headers) for key, index in supplied.items()):
        raise ApiError("Choose a file column for each mapped field.", "mapping")
    if len(set(supplied.values())) != len(supplied):
        raise ApiError("Use a different file column for each field.", "mapping")
    mode = data.get("amount_mode", "debit-negative")
    amount_fields = ("debit", "credit") if mode == "separate" else ("amount",)
    if not {"date", "name"} <= supplied.keys() or not any(field in supplied for field in amount_fields):
        raise ApiError("Map the date, description, and amount columns.", "mapping")
    return supplied


def _csv_date_order(rows, index):
    orders = set()
    for _, row in rows:
        match = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.]\d{4}", _pick(row, index))
        if match:
            first, second = map(int, match.groups())
            if 12 < first <= 31 and 1 <= second <= 12:
                orders.add("dmy")
            if 12 < second <= 31 and 1 <= first <= 12:
                orders.add("mdy")
    return next(iter(orders)) if len(orders) == 1 else "auto"


def _complete_csv_layout(headers, rows, suggestion):
    """Keep usable model fields and fill gaps using recognizable headers."""
    proposed = suggestion["mapping"]
    local = _map_columns(headers)
    if _normalized_header(_pick(headers, local.get("date"))) == "valuedate":
        local.pop("date")
    mode = suggestion["amount_mode"]
    signed_column = "amount" in proposed or "amount" in local
    dedicated_columns = any(field in proposed or field in local for field in ("debit", "credit"))
    if dedicated_columns and (mode == "separate" or not signed_column):
        mode = "separate"
    elif mode == "separate":
        mode = None
    modes = ("debit", "credit") if mode == "separate" else ("amount", "direction")
    mapping = {}
    for field in ("date", "name", *modes, "currency", "reference"):
        for candidate in (proposed.get(field), local.get(field)):
            if candidate is not None and candidate not in mapping.values():
                mapping[field] = candidate
                break
    date_order = suggestion["date_format"]
    if date_order == "auto" and "date" in mapping:
        date_order = _csv_date_order(rows, mapping["date"])
    if mode is None and {"amount", "direction"} <= mapping.keys() and all(
        not _pick(row, mapping["amount"]) or _pick(row, mapping["direction"]) for _, row in rows
    ):
        mode = "debit-negative"
    return {"mapping": mapping, "date_format": date_order, "amount_mode": mode}


def _openai_csv_layout(database, data, account_id, headers, rows, key_store, user_id):
    if data.get("ai_consent") is not True:
        raise ApiError("Confirm that Quant may send CSV headers and sample rows to OpenAI.", "ai_consent")
    with database.connection(snapshot=True) as conn:
        _set_account(conn, account_id)
    api_key, remember_key = _ai_key(data, key_store, user_id, validate_openai_key)
    count = min(8, len(rows))
    indices = [index * (len(rows) - 1) // (count - 1) for index in range(count)] if count > 1 else [0]
    samples = [[_clean(cell, 100) for cell in rows[index][1][:len(headers)]] for index in indices]
    suggestion = suggest_csv_mapping(headers, samples, api_key=api_key, model=data["openai_model"])
    layout = _complete_csv_layout(headers, rows, suggestion)
    notices = []
    if remember_key:
        _remember_ai_key(key_store, user_id, api_key, "OpenAI", notices)
    return layout, notices


def _review_prompt(database, data, today, key_store, user_id):
    if data.get("ai_consent") is not True:
        raise ApiError("Confirm that Quant may send the prompt and editable rows to OpenAI.", "ai_consent")
    prompt = line(data.get("prompt"), "prompt", 2000)
    model = validate_openai_model(data.get("openai_model", OPENAI_DEFAULT_MODEL))
    source = choice(data.get("source"), SOURCES, "source")
    account_id = identifier(data.get("account_id"), "account_id")
    supplied_rows = data.get("rows")
    if (not isinstance(supplied_rows, list) or not 1 <= len(supplied_rows) <= MAX_ROWS
            or any(not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1 for row in supplied_rows)):
        raise ApiError("Use a review with 1 to 4,000 editable rows for one prompt.", "rows")
    rows = []
    ids = set()
    for row in supplied_rows:
        if row["id"] in ids:
            raise ApiError("The table contains repeated row identifiers.", "rows")
        ids.add(row["id"])
        fields = {field: _clean(row[field], 150) for field in (
            "posted_on", "name", "amount", "category_id", "action", "target", "currency", "reference", "source", "line",
        ) if field in row}
        if row.get("csv_source"):
            fields["csv_source"] = _clean(row["csv_source"], 350)
        choices = row.get("choices")
        if isinstance(choices, dict):
            fields["choices"] = {
                action: [[_clean(pair[0], 60), _clean(pair[1], 150)] for pair in choices[action][:12]
                         if isinstance(pair, list) and len(pair) == 2]
                for action in ("link", "commitment", "expected") if isinstance(choices.get(action), list)
            }
        rows.append({"id": row["id"], **fields})
    with database.connection(snapshot=True) as conn:
        _set_account(conn, account_id)
        categories = [dict(row) for row in conn.execute("""
            SELECT id, name, kind, parent_id FROM categories WHERE id > 0 AND kind IN ('income', 'expense') ORDER BY name
        """)]
        targets = {} if source == "amazon" else {
            "transfer": [list(row) for row in conn.execute("SELECT id, name FROM accounts WHERE id > 0 AND id <> ?", (account_id,))],
            "settle": [list(row) for row in conn.execute("SELECT id, name FROM shared_people WHERE id > 0")],
        }
    context = {"rows": rows, "categories": categories, "default_categories": DEFAULT_IMPORT_CATEGORIES,
               "targets": targets, "source_type": source, "today": today.isoformat()}
    api_key, remember_key = _ai_key(data, key_store, user_id, validate_openai_key)
    result = suggest_review_edits(prompt, context, api_key=api_key, model=model)
    notices = []
    if remember_key:
        _remember_ai_key(key_store, user_id, api_key, "OpenAI", notices)
    if key_store:
        result["ai"] = _ai_configuration(key_store, user_id, "OpenAI")
    result["notices"] = notices
    return result


def _token(payload):
    body = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).decode()
    return body + "." + hmac.new(TOKEN_KEY, body.encode(), "sha256").hexdigest()


def _unpack(value):
    if isinstance(value, str) and len(value) <= 8192:
        try:
            body, signature = value.split(".", 1)
            expected = hmac.new(TOKEN_KEY, body.encode(), "sha256").hexdigest()
            if hmac.compare_digest(signature, expected):
                return json.loads(base64.urlsafe_b64decode(body.encode()))
        except (ValueError, UnicodeError):
            pass
    raise ApiError("Review the file again before importing these rows.", status=409)


def _set_account(conn, account_id):
    owned_account(account_id)
    account = row_by_id(conn, "accounts", account_id, "account_id")
    if account["kind"] == "investment":
        raise ApiError("Choose a checking, savings or cash account for statement imports.", "account_id")
    return account


def _adjacent(day, days):
    return (max(day.toordinal() - days, date.min.toordinal()), min(day.toordinal() + days, date.max.toordinal()))


def _statement_index(ledger, account_id, source):
    by_amount = defaultdict(list)
    for entry in ledger.entries:
        if source in BANK_SOURCES and not entry.get("shared") and entry.get("statement_expected") is False:
            continue
        for affected, moved in movements(entry):
            if affected == account_id and moved:
                by_amount[moved].append(entry)
    return by_amount


def _existing_matches(by_amount, cents, day, tolerance):
    start, end = map(lambda ordinal: date.fromordinal(ordinal).isoformat(), _adjacent(day, tolerance))
    return [row for row in by_amount[cents] if start <= row["posted_on"] <= end]


def _bank_claims(conn, account_id):
    return {row[0] for row in conn.execute("""
        SELECT entry_id FROM import_records WHERE account_id = ? AND source IN ('csv','pdf')
    """, (account_id,))}


def _matching_description(statement, recorded):
    def tokens(value):
        return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold())

    if not isinstance(statement, str) or not recorded:
        return False
    first, second = tokens(statement), tokens(recorded)
    if first == second:
        return any(word.isalpha() and word not in GENERIC_PAYMENT_WORDS for word in first)
    shorter, longer = sorted((first, second), key=len)
    if not any(len(word) >= 4 and word.isalpha() and word not in GENERIC_PAYMENT_WORDS for word in shorter):
        return False
    for index in range(len(longer) - len(shorter) + 1):
        end = index + len(shorter)
        if longer[index:end] == shorter:
            return all(word.isdigit() or word in GENERIC_PAYMENT_WORDS for word in longer[:index] + longer[end:])
    return False


def _exact_expense(matches, posted, cents, account, description):
    if len(matches) != 1 or cents >= 0 or account["kind"] not in ("checking", "savings"):
        return None
    entry = matches[0]
    if (entry["id"] > 0 and entry["kind"] == "expense" and entry["account_id"] == account["id"]
            and entry["posted_on"] == posted and entry.get("statement_expected") is True and not entry.get("shared")
            and _matching_description(description, entry["name"])):
        return entry["id"]
    return None


def _ready_stamp(action, posted, name, cents, entry_id=None):
    details = [posted, name, str(cents)]
    if action == "create":
        return _digest(details)
    if action == "link" and type(entry_id) is int:
        return _digest(["link", entry_id, *details])
    return None


def _commitment_matches(database, day, cents, account_id, today, conn, cache):
    key = day.isoformat()
    if key not in cache:
        low, high = map(date.fromordinal, _adjacent(day, 3))
        cache[key] = calendar_occurrences(database, low, min(high, today), conn)
    return [rule for rule in cache[key] if rule["status"] == "pending" and cents == _commitment_movement(rule, account_id)][:8]


def _commitment_movement(rule, account_id):
    if rule["account_id"] == account_id:
        return rule["amount"] if rule["kind"] == "income" else -rule["amount"]
    if rule["destination_id"] == account_id:
        return rule["amount"]
    return None


def _expected_matches(plans, day, cents, account_id):
    return [plan for plan, funded in plans if funded and plan["account_id"] == account_id
            and plan["signed_amount"] == cents and abs((date.fromisoformat(plan["due_on"]) - day).days) <= 7][:8]


def _review_group(row, source, verified_document=False):
    if row["existing"] is not None:
        return "handled"
    if row["blocked"] or row["errors"] or row.get("review_hold"):
        return "review"
    if row["auto_match"] is not None:
        return "ready"
    if any(not match["reconciled"] for match in row["matches"]) or row["commitments"] or row["expected"]:
        return "matches"
    return "ready" if (source == "csv" or verified_document) and not row["matches"] else "review"


def _openai_totals(extraction, rows):
    printed = {}
    for label in ("debit", "credit"):
        try:
            value = money(extraction.get("total_" + label, ""), "total")
            if value >= 0:
                printed[label] = value
        except ApiError:
            pass
    amounts = [int(row["amount"]) for row in rows if row["amount"] is not None and not row["blocked"]
               and not row["currency_unconfirmed"]]
    debit = -sum(min(value, 0) for value in amounts)
    credit = sum(max(value, 0) for value in amounts)
    calculated = {"debit": debit, "credit": credit}
    check = "not_printed"
    if printed:
        if any(calculated[label] != value for label, value in printed.items()):
            check = "mismatch"
        elif len(printed) == 2:
            check = "matched"
        else:
            check = "partial"
    return {
        "document_type": extraction.get("document_type", "unknown"), "totals_check": check,
        "printed_debits": str(printed["debit"]) if "debit" in printed else None,
        "printed_credits": str(printed["credit"]) if "credit" in printed else None,
        "extracted_debits": str(debit), "extracted_credits": str(credit),
        "note": _clean(extraction.get("document_note"), 200),
    }


def _merchant_key(value):
    name = unicodedata.normalize("NFKC", value).casefold()
    name = re.split(r"\b(?:loc|location|store|filiaal)\s+\d+\b", name, maxsplit=1)[0]
    words = re.findall(r"[^\W_]+", name)
    ignored = GENERIC_PAYMENT_WORDS | {"sumup", "ccv", "nld", "ita"}
    return " ".join(word for word in words if word not in ignored and not any(char.isdigit() for char in word))


class _CategoryHistory:
    def __init__(self, ledger, account_id):
        self.account_id = account_id
        self.categories = {
            item["id"]: {key: item[key] for key in ("id", "name", "kind", "parent_id")}
            for item in ledger.categories
            if item["id"] > 0 and item["kind"] in DEFAULT_IMPORT_CATEGORIES
            and not (item["parent_id"] is None
                     and item["name"].casefold() == DEFAULT_IMPORT_CATEGORIES[item["kind"]].casefold())
        }
        self.exact = defaultdict(list)
        self.merchants = defaultdict(list)
        self.related = defaultdict(list)
        for entry in reversed(ledger.entries):
            if entry["id"] <= 0 or entry["category_id"] not in self.categories:
                continue
            kind, name = entry["kind"], entry["name"]
            record = (entry["account_id"], entry["category_id"], name)
            self.exact[kind, _normalized_header(name)].append(record)
            merchant = _merchant_key(name)
            if merchant:
                self.merchants[kind, merchant].append(record)
                self.related[kind, merchant.split()[0]].append(record)

    def suggest(self, kind, name):
        merchant = _merchant_key(name)
        if not merchant:
            return None, False
        matches = self.exact.get((kind, _normalized_header(name))) or self.merchants.get((kind, merchant), ())
        local = [category for account, category, _ in matches if account == self.account_id]
        choices = set(local) if local else {category for _, category, _ in matches}
        return (next(iter(choices)), False) if len(choices) == 1 else (None, len(choices) > 1)

    def examples(self, kind, name):
        merchant = _merchant_key(name)
        if not merchant:
            return []
        examples = {}
        for _, category, description in self.related.get((kind, merchant.split()[0]), ()):
            examples.setdefault((description, category), {"name": description, "category_id": category})
            if len(examples) == 6:
                break
        return list(examples.values())


def _review_categories(database, data, key_store, user_id):
    if data.get("ai_consent") is not True:
        raise ApiError("Confirm that Quant may send descriptions and category examples to OpenAI.", "ai_consent")
    model = validate_openai_model(data.get("openai_model", OPENAI_DEFAULT_MODEL))
    account_id = identifier(data.get("account_id"), "account_id")
    supplied = data.get("rows")
    if not isinstance(supplied, list) or not 1 <= len(supplied) <= MAX_CATEGORY_ROWS:
        raise ApiError("Choose between 1 and 120 rows for category suggestions.", "rows")
    with database.connection(snapshot=True) as conn:
        _set_account(conn, account_id)
        history = _CategoryHistory(database.ledger(conn), account_id)
    suggestions, pending, ids = [], {}, set()
    for row in supplied:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1 or row["id"] in ids:
            raise ApiError("Choose distinct draft rows for category suggestions.", "rows")
        ident = row["id"]
        ids.add(ident)
        name = line(row.get("name"), "name", 100)
        cents = money(row.get("amount"), "amount")
        if not cents:
            raise ApiError("Enter a nonzero amount before suggesting a category.", "amount")
        kind = "expense" if cents < 0 else "income"
        category, conflict = history.suggest(kind, name)
        if category is not None:
            suggestions.append({"id": ident, "category_id": category})
        elif not conflict:
            pending[ident] = {"id": ident, "name": name, "kind": kind, "examples": history.examples(kind, name)}
    response = {"suggestions": suggestions, "notices": []}
    if pending and history.categories:
        api_key, remember_key = _ai_key(data, key_store, user_id, validate_openai_key)
        instruction = ("Suggest only category_id edits. Prefer consistent past examples for the same merchant. "
                       "Without relevant history, assign a category only if the description clearly identifies the activity "
                       "and exactly one supplied category fits its kind. Leave any ambiguous merchant, payment processor, "
                       "or conflicting history unchanged. List the specific row IDs for each suggestion. Do not change other fields.")
        context = {"rows": list(pending.values()), "categories": list(history.categories.values())}
        edited = suggest_review_edits(instruction, context, api_key=api_key, model=model)
        proposed = {}
        for update in edited["updates"]:
            if set(update) != {"rows", "category_id"} or update["rows"] is None:
                continue
            value = update["category_id"]
            category = history.categories.get(int(value)) if re.fullmatch(r"[0-9]{1,18}", value) else None
            if category:
                for ident in update["rows"]:
                    if ident in pending and pending[ident]["kind"] == category["kind"]:
                        proposed.setdefault(ident, set()).add(category["id"])
        suggestions.extend({"id": ident, "category_id": next(iter(categories))}
                           for ident, categories in proposed.items() if len(categories) == 1)
        if remember_key:
            _remember_ai_key(key_store, user_id, api_key, "OpenAI", response["notices"])
    if key_store:
        response["ai"] = _ai_configuration(key_store, user_id, "OpenAI")
    return response


def _preview(database, data, today, *, key_store=None, user_id=None):
    source = choice(data.get("source"), SOURCES, "source")
    method = _pdf_method(data)
    csv_openai = _csv_method(data) == "openai"
    ai = method != "local"
    openai = method == "openai"
    if openai or csv_openai:
        data = {**data, "openai_model": validate_openai_model(data.get("openai_model", OPENAI_DEFAULT_MODEL))}
    account_id = identifier(data.get("account_id"), "account_id")
    batch_name = _batch_name(data, source, today)
    document = None
    extraction = None
    if ai:
        headers, raw_rows, extracted, ai_notices, original_pdf, document, extraction = _ai_pdf(
            data, database, account_id, key_store, user_id, method, batch_name, today,
        )
        uncertain = []
    else:
        _, headers, raw_rows, extracted, uncertain, original_pdf = _extract(data)
        ai_notices = []
    if len(raw_rows) > MAX_ROWS:
        raise ApiError("Split the file into batches of up to 4,000 transactions.", "file")
    if csv_openai:
        layout, csv_notices = _openai_csv_layout(database, data, account_id, headers, raw_rows, key_store, user_id)
        data = {**data, **layout}
    mapping = layout["mapping"] if csv_openai else _mapping(data, headers, source)
    date_order = "ymd" if ai or source == "amazon" else choice(data.get("date_format", "auto"), DATE_FIELDS, "date_format")
    if csv_openai:
        mode = layout["amount_mode"]
    else:
        mode = "debit-negative" if ai or source == "amazon" else choice(
            data.get("amount_mode", "debit-negative"), AMOUNT_MODES, "amount_mode")
    amount_fields = ("debit", "credit") if mode == "separate" else ("amount",)
    amount_mapped = any(field in mapping for field in amount_fields)
    warnings = set(uncertain)
    with database.connection(snapshot=True) as conn:
        account = _set_account(conn, account_id)
        ledger = database.ledger(conn)
        category_history = _CategoryHistory(ledger, account_id)
        by_amount = _statement_index(ledger, account_id, source)
        handled = {row["fingerprint"]: row["entry_id"] for row in conn.execute(
            "SELECT fingerprint, entry_id FROM import_records WHERE account_id = ?", (account_id,)
        )}
        matched = {row[0] for row in conn.execute(
            "SELECT entry_id FROM import_records WHERE account_id = ? AND source = ?", (account_id, source)
        )}
        bank_claimed = _bank_claims(conn, account_id) if source in BANK_SOURCES else set()
        expected = list(_expected_status(conn))
        seen = Counter()
        statement_dates = Counter()
        cache = {}
        pending = []
        for index, row in raw_rows:
            evidence = extraction["rows"][index - 1] if openai else {}
            value = _pick(row, mapping.get("date"))
            statement_description = _clean(_pick(row, mapping.get("name")), 600 if openai else 100)
            description = statement_description[:100].rstrip()
            name = description or (batch_name if source == "pdf" else "")
            reference = _clean(_pick(row, mapping.get("reference")), 150)
            currency = _pick(row, mapping.get("currency")).upper()
            posted = None
            cents = None
            errors = []
            if issue := _clean(evidence.get("issue"), 200):
                errors.append(issue)
            try:
                if csv_openai and "date" not in mapping:
                    raise ApiError("No posting date column was identified.", "date")
                posted = parse_date(value, date_order)
                if posted > today:
                    errors.append("Enter today’s date or an earlier calendar date.")
            except ApiError as error:
                errors.append(str(error))
            try:
                if csv_openai and not amount_mapped:
                    raise ApiError("No transaction amount column was identified.", "amount")
                if csv_openai and mode is None:
                    raise ApiError("Confirm which amounts are debits or credits.", "amount")
                cents = _column_amount(row, mapping, mode)
                if not cents or cents == -(2**63):
                    errors.append("Enter a nonzero amount within the supported range.")
            except ApiError as error:
                if source == "amazon" and index in warnings:
                    errors.append("Confirm this order’s charged total from your payment statement.")
                else:
                    errors.append(str(error))
            if not name:
                errors.append("Add a description.")
            blocked = bool(currency and currency not in ("EUR", "€"))
            currency_unconfirmed = not currency and (ai or (csv_openai and mapping.get("currency") is not None))
            if blocked:
                errors.append("This row is not in euros.")
            if currency_unconfirmed or (source == "csv" and mapping.get("currency") is not None and not currency):
                errors.append("Confirm that this row is in euros.")
            if source == "amazon" and not currency:
                errors.append("Confirm that this order was charged in euros.")
            if source in BANK_SOURCES and posted is not None and cents:
                statement_dates[cents, posted.toordinal()] += 1

            fingerprint_name = statement_description or ("PDF payment" if source == "pdf" else "")
            signature = [posted.isoformat() if posted else value, fingerprint_name.casefold(), cents]
            seen_key = tuple(signature) if posted and name and cents else ("raw", _digest(row))
            seen[seen_key] += 1
            identified = _normalized_header(reference) not in ("", "notprovided", "na", "none")
            fingerprint = (
                _digest(["order", reference]) if source == "amazon" and reference else
                _digest(["bank-reference", reference]) if identified else
                _digest([*seen_key, seen[seen_key]])
            )
            saved = handled.get(fingerprint)
            match_rows, rule_matches, repayment_matches = [], [], []
            automatic = None
            if not saved and (not errors or (currency_unconfirmed and len(errors) == 1)):
                matches = _existing_matches(by_amount, cents, posted, 7 if source == "amazon" else 3)
                matches.sort(key=lambda entry: (entry["id"] in matched, abs((date.fromisoformat(entry["posted_on"]) - posted).days), entry["id"]))
                match_rows = [
                    {key: entry[key] for key in ("id", "name", "posted_on", "kind")} | {
                        "amount": str(cents), "reconciled": entry["id"] in matched,
                        "shared": entry.get("shared"),
                        "personal_amount": str(entry["amount"]) if entry.get("shared", {}).get("type") == "payment" else None,
                        "shared_delta": str(entry["shared_delta"]) if "shared_delta" in entry else None,
                    }
                    for entry in matches[:12]
                ]
                if source in BANK_SOURCES:
                    rule_matches = [
                        {"id": rule["id"], "name": rule["name"], "date": rule["date"].isoformat(),
                         "amount": str(_commitment_movement(rule, account_id)), "kind": rule["kind"]}
                        for rule in _commitment_matches(database, posted, cents, account_id, today, conn, cache)
                    ]
                    repayment_matches = [
                        {"id": plan["id"], "date": plan["due_on"], "tag": plan["tag"], "amount": str(plan["signed_amount"])}
                        for plan in _expected_matches(expected, posted, cents, account_id)
                    ]
                    if description and not errors and not warnings and not ai_notices and not rule_matches and not repayment_matches:
                        candidate = _exact_expense(matches, posted.isoformat(), cents, account, description)
                        if candidate not in bank_claimed:
                            automatic = candidate

            category, category_conflict = category_history.suggest("expense" if cents < 0 else "income", description) if cents and description else (None, False)
            pending.append(({
                "line": index, "name": name, "posted_on": posted.isoformat() if posted else "",
                "amount": None if cents is None else str(cents), "reference": reference,
                "currency": currency if currency_unconfirmed else currency or "EUR",
                "currency_unconfirmed": currency_unconfirmed,
                "errors": errors, "blocked": blocked, "existing": saved, "matches": match_rows,
                "commitments": rule_matches, "expected": repayment_matches,
                "suggested_category_id": category, "category_conflict": category_conflict,
                "page": evidence.get("page"), "evidence": _clean(evidence.get("evidence"), 160),
                "statement_description": statement_description if openai else "",
                **({"csv_source": " · ".join(f"{header}: {_clean(cell, 100)}" for header, cell in zip(headers, row)
                                            if cell.strip())[:350]} if csv_openai and errors else {}),
            }, fingerprint, seen_key, identified, automatic))
    copies = Counter(item[1] for item in pending)
    if document is None and original_pdf is not None:
        document = archive_statement_pdf(database, account_id, data.get("filename"), batch_name, original_pdf, today)
    normalized = []
    workspace_scope = _workspace_scope(database)
    for row, fingerprint, signature, identified, automatic in pending:
        if copies[fingerprint] > 1:
            row["errors"].append("More than one row has the same reference. Review the intended transaction.")
        elif not identified and seen[signature] > 1:
            row["errors"].append("More than one row has the same date, description and amount. Review each row.")
        if automatic is not None and not row["errors"]:
            low, high = _adjacent(date.fromisoformat(row["posted_on"]), 3)
            if sum(statement_dates[int(row["amount"]), day] for day in range(low, high + 1)) != 1:
                automatic = None
        else:
            automatic = None
        row.update(auto_match=automatic, review_key=fingerprint, reliable_reference=identified,
                   document_id=document["id"] if document else None)
        normalized.append(row)
    totals = _openai_totals(extraction, normalized) if openai else None
    totals_mismatch = bool(totals and totals["totals_check"] == "mismatch")
    verified = bool(totals and totals["document_type"] == "statement" and totals["totals_check"] == "matched"
                    and not totals["note"] and not ai_notices
                    and all(not row["errors"] and not row["blocked"] and row["page"] and row["evidence"]
                            for row in normalized))
    for row in normalized:
        if totals_mismatch:
            row.update(auto_match=None, review_hold=True)
        group = _review_group(row, source, verified)
        automatic = row["auto_match"]
        action = "link" if automatic is not None else "create"
        stamp = _ready_stamp(action, row["posted_on"], row["name"], row["amount"], automatic) if group == "ready" else None
        row.update(review_group=group, token=_token([
            source, account_id, row["review_key"], row["reference"], row["blocked"], stamp,
            row["currency_unconfirmed"], workspace_scope, row["document_id"], row["page"], row["evidence"],
            row["statement_description"],
        ]))
    notices = []
    if csv_openai:
        missing = []
        if "date" not in mapping:
            missing.append("posting date")
        if "name" not in mapping:
            missing.append("description")
        if not amount_mapped:
            missing.append("amount")
        if missing:
            notices.append("The CSV layout is missing " + ", ".join(missing) + ". Fill the blank cells in the review table.")
        if amount_mapped and mode is None:
            notices.append("The amount signs are unclear. Enter signed amounts in the review table.")
        notices.append("Check the parsed dates and amounts against your statement. To adjust the layout, choose Manual column mapping in Import settings.")
        notices.extend(csv_notices)
    if source == "pdf":
        if data.get("ocr") is True and not openai:
            notices.append("Review dates, decimals, and debit or credit signs against the scan. OCR may misread characters.")
        if ai:
            provider = "OpenAI" if openai else "Groq"
            notices.append(f"{provider} extracted these rows. Compare dates, amounts, and debit or credit signs with the original PDF.")
            notices.extend(ai_notices)
            if not normalized:
                notices.append(f"{provider} found no transactions. Try a bank CSV if you have one.")
            if totals_mismatch:
                notices.append("Extracted debits or credits differ from the reported printed totals. Review the affected rows against the PDF before applying them.")
        elif not extracted.strip():
            notices.append("No transaction text was found. Enable local OCR for a scan or request a CSV statement.")
        elif uncertain:
            notices.append(f"Skipped {len(uncertain)} PDF lines with unclear amount or balance columns. Compare the extracted text with your statement.")
        elif not normalized:
            notices.append("No supported date and amount layout was recognized. Download the text for review or use a CSV statement.")
    if source == "amazon":
        notices.append("Check order totals against your bank. One order can involve gift cards, refunds, or several charges.")
    result = {"source": source, "rows": normalized, "notices": notices, "extracted": extracted,
              "name": batch_name, "pdf_method": method if source == "pdf" else None,
              "summary": import_report(normalized)}
    if csv_openai:
        result["csv_method"] = "openai"
        result["csv_layout"] = {"headers": headers, "sample": [row[:len(headers)] for _, row in raw_rows[:6]], **layout}
    if openai or csv_openai:
        result["openai_model"] = data["openai_model"]
    if (ai or csv_openai) and key_store:
        result["ai"] = _ai_configuration(key_store, user_id, "OpenAI" if openai or csv_openai else "Groq")
    if totals is not None:
        result["extraction"] = totals
    if document is not None:
        result["document"] = document
    return result


def _inspect(data):
    if _pdf_method(data) != "local":
        raise ApiError("Use Review transactions to extract a PDF with an AI provider.", "pdf_method")
    if _csv_method(data) != "local":
        raise ApiError("Use Review transactions for OpenAI CSV mapping.", "csv_method")
    source, headers, rows, extracted, uncertain, _ = _extract(data)
    return {
        "source": source, "headers": headers, "mapping": _map_columns(headers),
        "sample": [row[:len(headers)] for _, row in rows[:6]], "count": len(rows),
        "extracted": extracted, "uncertain": len(uncertain),
    }


def _movement_entry(rule, amount, name, posted, category=None):
    return {
        "kind": rule["kind"], "name": name, "posted_on": posted, "amount": euros(abs(amount)),
        "account_id": rule["account_id"], "destination_id": rule["destination_id"],
        "category_id": category or rule["category_id"],
    }


def _default_import_category(conn, kind):
    name = DEFAULT_IMPORT_CATEGORIES[kind]
    category = conn.execute("""
        SELECT id FROM categories WHERE id > 0 AND kind = ? AND parent_id IS NULL AND name = ?
    """, (kind, name)).fetchone()
    if category:
        return category["id"]
    return conn.execute("""
        INSERT INTO categories (kind, name, color) VALUES (?, ?, 'slate')
    """, (kind, name)).lastrowid


def _extend_import_history(conn, posted, account_id, destination_id=None):
    owned_account(account_id)
    owned_account(destination_id, "destination_id")
    conn.execute("""
        UPDATE accounts SET opened_on = ? WHERE id IN (?, ?) AND opened_on > ?
    """, (posted, account_id, destination_id, posted))


def _make_entry(database, conn, today, pending, ledger, scope):
    source, account_id, _, reference, blocked, _, currency_unconfirmed, *_ = scope
    if blocked:
        raise ApiError("Only rows in euros can be posted.", "amount")
    if currency_unconfirmed and pending.get("currency_confirmed") is not True:
        raise ApiError("Confirm that this row is in euros.", "currency_confirmed")
    posted = recorded_date(pending.get("posted_on"), "posted_on", today)
    day = date.fromisoformat(posted)
    cents = money(pending.get("amount"), "amount")
    if not cents or cents == -(2**63):
        raise ApiError("Enter a nonzero amount within the supported range.", "amount")
    name = line(pending.get("name"), "name", 100)
    action = choice(pending.get("action"), ("create", "link", "transfer", "commitment", "expected", "settle"), "action")
    category_value = pending.get("category_id", "default" if action == "create" else None)
    default_category = action == "create" and category_value == "default"
    category = None if default_category else identifier(category_value, "category_id")
    _set_account(conn, account_id)
    note = "Amazon order " + reference if source == "amazon" and reference else ""
    labels = ["amazon"] if source == "amazon" else []
    if action == "link":
        entry_id = pending.get("entry_id")
        if type(entry_id) is not int or not any(
            row["id"] == entry_id for row in _existing_matches(_statement_index(ledger(), account_id, source), cents, day, 7 if source == "amazon" else 3)
        ):
            raise ApiError("The existing transaction no longer matches. Review the file again.", status=409)
        if conn.execute("SELECT 1 FROM import_records WHERE account_id = ? AND source = ? AND entry_id = ?",
                        (account_id, source, entry_id)).fetchone():
            raise ApiError("That transaction already matches another row from this source.", status=409)
        return entry_id, action

    if source == "amazon" and (action != "create" or cents >= 0):
        raise ApiError("Amazon orders can link an existing charge or be recorded as confirmed purchases.", "action")
    if action == "expected":
        expected = identifier(pending.get("expected_id"), "expected_id")
        plans = list(_expected_status(conn))
        if not any(row["id"] == expected for row in _expected_matches(plans, day, cents, account_id)):
            raise ApiError("The scheduled settlement no longer matches. Review the file again.", status=409)
        _extend_import_history(conn, posted, account_id)
        return -2 * _record_expected(conn, expected, {"posted_on": posted}, today) - 1, action
    if action == "settle":
        _extend_import_history(conn, posted, account_id)
        settlement_id = _settle(conn, {
            "person_id": pending.get("person_id"), "account_id": account_id, "posted_on": posted,
            "amount": euros(abs(cents)), "direction": "receive" if cents > 0 else "pay", "note": name,
        }, today)
        return -2 * settlement_id - 1, action
    if action == "commitment":
        rule_id = identifier(pending.get("rule_id"), "rule_id")
        due = recorded_date(pending.get("due_on"), "due_on", today)
        target = next((rule for rule in _commitment_matches(database, day, cents, account_id, today, conn, {})
                       if rule["id"] == rule_id and rule["date"].isoformat() == due), None)
        if target is None:
            raise ApiError("The scheduled commitment no longer matches. Review the file again.", status=409)
        entry = _movement_entry(target, cents, name, posted, category)
    elif action == "transfer":
        other = identifier(pending.get("other_account_id"), "other_account_id")
        source_id, destination_id = (account_id, other) if cents < 0 else (other, account_id)
        origin = row_by_id(conn, "accounts", source_id, "other_account_id")
        destination = row_by_id(conn, "accounts", destination_id, "other_account_id")
        kind = "investment" if destination["kind"] == "investment" and origin["kind"] != "investment" else "transfer"
        entry = _movement_entry({"kind": kind, "account_id": source_id, "destination_id": destination_id, "category_id": None},
                                cents, name, posted, category if kind == "investment" else None)
    else:
        kind = "income" if cents > 0 else "expense"
        entry = {
            "kind": kind, "name": name, "posted_on": posted, "amount": euros(abs(cents)),
            "account_id": account_id, "category_id": _default_import_category(conn, kind) if default_category else category,
        }
    _extend_import_history(conn, posted, entry["account_id"], entry.get("destination_id"))
    entry_id = database.save_entry(dict(entry, note=note, labels=labels), today, conn=conn)
    if action == "commitment":
        post_occurrence(database, {"rule_id": rule_id, "date": due, "entry_id": entry_id}, today, conn=conn)
    return entry_id, action


def _commit(database, data, today):
    selected = data.get("rows")
    if not isinstance(selected, list) or not 1 <= len(selected) <= MAX_ROWS or not all(isinstance(row, dict) for row in selected):
        raise ApiError("Select between 1 and 4,000 reviewed rows.", "rows")
    scopes = [_unpack(row.get("token")) for row in selected]
    source, account_id, *_ = scopes[0]
    workspace_scope = _workspace_scope(database)
    if any(scope[0] != source or scope[1] != account_id or scope[7] != workspace_scope for scope in scopes):
        raise ApiError("Review the file in this workspace before importing its rows.", "account_id", 409)
    batch_name = _batch_name(data, source, today)
    ready_only = data.get("ready_only") is True
    verify_ready = [ready_only or row.get("verify_ready") is True for row in selected]
    has_verified_rows = any(verify_ready)
    saved, already = 0, 0
    results = []
    with database.connection(write=True) as conn:
        batch_id = None
        # A row may match a transfer or settlement posted earlier in this batch.
        ledger = lambda: database.ledger(conn)
        if has_verified_rows:
            bulk_account = _set_account(conn, account_id)
            bulk_index = _statement_index(ledger(), account_id, source)
            bulk_plans = list(_expected_status(conn))
            bulk_occurrences = {}
            bulk_claimed = _bank_claims(conn, account_id)
            batch_dates = defaultdict(set)
            for row, scope in zip(selected, scopes):
                if conn.execute("SELECT 1 FROM import_records WHERE account_id=? AND fingerprint=?",
                                (account_id, scope[2])).fetchone():
                    continue
                posted = date.fromisoformat(recorded_date(row.get("posted_on"), "posted_on", today))
                cents = money(row.get("amount"), "amount")
                batch_dates[cents, posted.toordinal()].add(scope[2])
        for row, scope, verify in zip(selected, scopes, verify_ready):
            (_, _, fingerprint, reference, blocked, stamp, currency_unconfirmed, _,
             document_id, page, evidence, description) = scope
            previous = conn.execute("""
                SELECT entry_id, action FROM import_records WHERE account_id = ? AND fingerprint = ?
            """, (account_id, fingerprint)).fetchone()
            if previous:
                results.append(dict(previous, existing=True))
                already += 1
                continue
            if verify:
                posted = date.fromisoformat(recorded_date(row.get("posted_on"), "posted_on", today))
                cents = money(row.get("amount"), "amount")
                action = row.get("action")
                name = row.get("name")
                signed = _ready_stamp(action, posted.isoformat(), name, cents, row.get("entry_id"))
                matches = _existing_matches(bulk_index, cents, posted, 3)
                valid = False
                if action == "create" and source in BANK_SOURCES:
                    valid = not matches
                if action == "link" and source in BANK_SOURCES:
                    candidate = _exact_expense(matches, posted.isoformat(), cents, bulk_account, name)
                    low, high = _adjacent(posted, 3)
                    siblings = set().union(*(batch_dates[cents, day] for day in range(low, high + 1)))
                    valid = (candidate is not None and candidate == row.get("entry_id")
                             and candidate not in bulk_claimed and len(siblings) == 1)
                if (not valid or blocked or currency_unconfirmed or stamp is None or stamp != signed
                    or _commitment_matches(database, posted, cents, account_id, today, conn, bulk_occurrences)
                    or _expected_matches(bulk_plans, posted, cents, account_id)):
                    raise ApiError("A row is no longer ready for bulk approval. Review the file again.", status=409)
            if batch_id is None:
                batch_id = conn.execute(
                    "INSERT INTO import_batches(source, account_id, count, name) VALUES(?, ?, 0, ?)",
                    (source, account_id, batch_name),
                ).lastrowid
            try:
                if verify and row["action"] == "link":
                    entry_id, action = row["entry_id"], "link"
                else:
                    entry_id, action = _make_entry(database, conn, today, row, ledger, scope)
            except ApiError as error:
                raise ApiError("Row " + str(row.get("line", saved + already + 1))[:12] + " · " + str(error), error.field, error.status) from error
            if has_verified_rows and action == "link":
                bulk_claimed.add(entry_id)
            conn.execute("""
                INSERT INTO import_records(account_id, fingerprint, source, batch_id, entry_id, action) VALUES (?, ?, ?, ?, ?, ?)
            """, (account_id, fingerprint, source, batch_id, entry_id, action))
            conn.execute("""
                INSERT INTO import_details(account_id, fingerprint, posted_on, name, amount, reference)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (account_id, fingerprint, row["posted_on"], row["name"].strip(), str(money(row["amount"], "amount")), reference))
            if document_id and conn.execute("SELECT 1 FROM statement_documents WHERE id=? AND account_id=?",
                                            (document_id, account_id)).fetchone():
                conn.execute("""
                    INSERT INTO import_document_rows(account_id, fingerprint, document_id, page, evidence, statement_description)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (account_id, fingerprint, document_id, page, evidence, description))
            results.append({"entry_id": entry_id, "action": action, "existing": False})
            saved += 1
        if batch_id:
            conn.execute("UPDATE import_batches SET count = ? WHERE id = ?", (saved, batch_id))
    return {"batch_id": batch_id, "saved": saved, "already": already, "rows": results, "name": batch_name}


def _ai_configuration(key_store, user_id, provider="Groq"):
    server_key = (openai_configured if provider == "OpenAI" else ai_configured)()
    state = {"configured": server_key, "provider": provider}
    if key_store:
        saved_key = key_store.has(user_id)
        state.update(configured=key_store.available if saved_key else server_key,
                     saved_key=saved_key, server_key=server_key, can_save_key=key_store.available)
    return state


def _setup(database, today, key_store, user_id, openai_store):
    ai = _ai_configuration(key_store, user_id)
    with database.connection(snapshot=True) as conn:
        return {
            "today": today.isoformat(),
            "accounts": [dict(row) for row in conn.execute("SELECT id, name, kind, opened_on FROM accounts WHERE id>0 ORDER BY name")],
            "categories": [dict(row) for row in conn.execute("SELECT * FROM categories WHERE id>0 ORDER BY name")],
            "default_categories": DEFAULT_IMPORT_CATEGORIES,
            "openai_models": OPENAI_MODELS,
            "openai_default_model": OPENAI_DEFAULT_MODEL,
            "people": [dict(row) for row in conn.execute("SELECT id, name, tag FROM shared_people ORDER BY name")],
            "history": [dict(row) for row in conn.execute("""
                SELECT import_batches.*, accounts.name AS account
                FROM import_batches LEFT JOIN accounts ON accounts.id = import_batches.account_id
                ORDER BY import_batches.id DESC LIMIT 20
            """)],
            "pdf_enabled": _pdf_backend() is not None,
            "ocr_enabled": _ocr_enabled(),
            "ai": ai,
            "openai": _ai_configuration(openai_store, user_id, "OpenAI"),
        }


def _batch(database, batch_id, query):
    try:
        page = int(query.get("page", "1"))
        if not 1 <= page <= (MAX_ROWS + 39) // 40:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise ApiError("Choose a valid import history page.", "page") from error
    with database.connection(snapshot=True) as conn:
        batch = row_by_id(conn, "import_batches", batch_id)
        rows = [dict(row) for row in conn.execute("""
            SELECT r.entry_id, r.action, d.posted_on, d.name, d.amount, d.reference,
                   s.document_id, s.page, s.evidence, s.statement_description, e.kind
            FROM import_records r
            LEFT JOIN import_details d USING (account_id, fingerprint)
            LEFT JOIN import_document_rows s USING (account_id, fingerprint)
            LEFT JOIN entries e ON e.id = r.entry_id
            WHERE r.batch_id = ? ORDER BY r.rowid
        """, (batch_id,))]
        documents = [dict(row) for row in conn.execute("""
            SELECT DISTINCT document.id, document.filename, document.name
            FROM import_records r JOIN import_document_rows s USING (account_id, fingerprint)
            JOIN statement_documents document ON document.id = s.document_id
            WHERE r.batch_id = ? ORDER BY document.id
        """, (batch_id,))]
        start = (page - 1) * 40
        return {"rows": rows[start:start + 40], "total": batch["count"], "page": page, "batch": dict(batch),
                "report": import_report(rows), "documents": documents}


def handle_imports(handler, path, query, method, today):
    if not path.startswith("/api/imports/"):
        return None
    database = getattr(handler, "database", handler.server.database)
    resource = path.removeprefix("/api/imports/")
    user = getattr(handler, "user", None)
    user_id = user["id"] if user else None
    key_store = getattr(handler.server, "groq_keys", None) if user else None
    openai_store = getattr(handler.server, "openai_keys", None) if user else None
    if resource == "setup" and method == "GET":
        return _setup(database, today, key_store, user_id, openai_store), 200
    if resource in ("groq-key", "openai-key") and method in ("PUT", "DELETE"):
        is_openai = resource == "openai-key"
        provider = "OpenAI" if is_openai else "Groq"
        store = openai_store if is_openai else key_store
        if store is None:
            raise ApiError(f"Sign in to manage your {provider} key.", status=401)
        if method == "PUT":
            validate = validate_openai_key if is_openai else validate_api_key
            store.save(user_id, validate(handler.read_data(4096).get("api_key")))
        else:
            store.remove(user_id)
        return {"ai": _ai_configuration(store, user_id, provider)}, 200
    batch = re.fullmatch(r"batch/([1-9][0-9]{0,17})", resource)
    if batch and method == "GET":
        return _batch(database, int(batch[1]), query), 200
    if method == "POST":
        if resource == "review-categories":
            return _review_categories(database, handler.read_data(128 * 1024), openai_store, user_id), 200
        if resource == "review-prompt":
            return _review_prompt(database, handler.read_data(MAX_REVIEW_PROMPT), today, openai_store, user_id), 200
        if resource == "inspect":
            return _inspect(handler.read_data((MAX_FILE + 2) // 3 * 4 + 20_000)), 200
        if resource == "preview":
            data = handler.read_data((MAX_FILE + 2) // 3 * 4 + 20_000)
            use_openai = data.get("pdf_method") == "openai" or data.get("csv_method") == "openai"
            store = openai_store if use_openai else key_store
            return _preview(database, data, today, key_store=store, user_id=user_id), 200
        if resource == "commit":
            return _commit(database, handler.read_data(MAX_COMMIT), today), 201
    raise ApiError("This import action was not found.", status=404)
