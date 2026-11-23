import json
import os
import unittest
from unittest.mock import patch

from apps.web.import_ai import HOST, MAX_RESPONSE_BYTES, MAX_TEXT_BYTES, MODEL, PATH, configured, extract_statement
from apps.web.values import ApiError


KEY = "test-groq-token"
STATEMENT = (
    "IBAN: NL12 ABNA 1234 5678 09\n"
    "Account number\n"
    "2944024494\n"
    "Booking date   Description   Amount EUR\n"
    "11/11/2026     Polder Market NL99 TEST 1234 5678 09  -12,34\n"
    "12/11/2026     Employer  +1.000,07"
)


def row(source, description="Polder Market", **changes):
    return {"source_line": source, "date": "2026-11-11", "description": description,
            "amount": "-12.34", "currency": "EUR", "reference": None, **changes}


def provider_output(rows, reason="stop"):
    value = json.dumps({"rows": rows}, ensure_ascii=False)
    return json.dumps({"choices": [{"finish_reason": reason, "message": {"content": value}}]}).encode()


class Response:
    def __init__(self, body=b"", status=200):
        self.body = body
        self.status = status
        self.limit = None

    def read(self, limit):
        self.limit = limit
        return self.body[:limit]


class GroqImportTests(unittest.TestCase):
    def setUp(self):
        transport = patch("apps.web.import_ai.HTTPSConnection")
        self.https = transport.start()
        self.addCleanup(transport.stop)
        self.connection = self.https.return_value

    def extract(self, statement=STATEMENT, rows=None, **kwargs):
        self.connection.getresponse.return_value = Response(provider_output(rows or []))
        return extract_statement(statement, api_key=KEY, **kwargs)

    def test_strict_schema_and_redaction_only_for_statement_holder_ids(self):
        actual = self.extract(rows=[row(5), row(6, "Employer", date="2026-11-12", amount="+1000.07", reference="PAY123")])
        self.assertEqual(actual["rows"], [
            {"date": "2026-11-11", "description": "Polder Market", "amount": "-12.34", "currency": "EUR", "reference": ""},
            {"date": "2026-11-12", "description": "Employer", "amount": "1000.07", "currency": "EUR", "reference": "PAY123"},
        ])
        self.https.assert_called_once_with(HOST, timeout=30)
        (method, path), request = self.connection.request.call_args
        self.assertEqual((method, path), ("POST", PATH))
        self.assertEqual(request["headers"]["Authorization"], "Bearer " + KEY)
        body = json.loads(request["body"])
        self.assertEqual(body["model"], MODEL)
        self.assertNotIn("tools", body)
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertNotIn(KEY, request["body"].decode())
        statement = body["messages"][1]["content"]
        self.assertNotIn("NL12", statement)
        self.assertNotIn("2944024494", statement)
        self.assertIn("NL99 TEST 1234 5678 09", statement)
        self.connection.close.assert_called_once()

    def test_missing_or_uncertain_bookings_remain_visible_in_source_order(self):
        result = self.extract(rows=[row(6, None, date="2026-02-29", amount="4307.50", currency=None)])
        self.assertEqual(len(result["rows"]), 2)
        first, second = result["rows"]
        self.assertIn("statement line 5", first["description"])
        self.assertEqual(first["amount"], "")
        self.assertEqual(second, dict.fromkeys(("date", "description", "amount", "currency", "reference"), ""))
        self.assertTrue(any("dated lines" in message for message in result["notices"]))
        self.assertTrue(any("2 rows" in message for message in result["notices"]))

    def test_duplicate_amount_bookings_retained_and_non_eur_not_converted(self):
        result = self.extract(
            statement="25/11/2026 Merchant 5.00\n25/11/2026 Merchant 5.00",
            rows=[row(2, "Merchant", date="2026-11-25", amount="-5.0", currency="usd"),
                  row(1, "Merchant", date="2026-11-25", amount="-5.00", currency="usd")],
        )
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0], result["rows"][1])
        self.assertEqual((result["rows"][0]["currency"], result["rows"][0]["amount"]), ("USD", "-5.00"))

    def test_balance_only_lines_do_not_create_placeholders(self):
        result = self.extract(
            statement="11/11/2026 Opening balance 600,20\n12/11/2026 Market EUR -1,30\n30/11/2026 Eindsaldo 598,90",
            rows=[row(2, "Market", date="2026-11-12", amount="-1.30")],
        )
        self.assertEqual(len(result["rows"]), 1)

    def test_paid_receipt_and_unpaid_invoice_remain_single_review_rows(self):
        paid = self.extract(
            statement="Market duplicate receipt\n12/11/2026\nTotal paid EUR 49.99",
            rows=[row(3, "Market", date="2026-11-12", amount="-49.99")],
        )
        self.assertEqual(len(paid["rows"]), 1)
        self.assertEqual(paid["rows"][0]["amount"], "-49.99")
        body = json.loads(self.connection.request.call_args.kwargs["body"])
        self.assertIn("extract one debit for the amount paid", body["messages"][0]["content"])
        unpaid = self.extract(
            statement="Market invoice awaiting payment\nDue on 12/11/2026\nAmount due EUR 49.99",
            rows=[row(3, "Market", date=None, amount=None)],
        )
        self.assertEqual(len(unpaid["rows"]), 1)
        self.assertEqual(unpaid["rows"][0]["amount"], "")
        self.assertEqual(unpaid["rows"][0]["date"], "")
        self.assertTrue(unpaid["notices"])

    def test_truncated_malformed_or_unbounded_responses_are_rejected(self):
        payloads = [
            provider_output([row(5)], "length"),
            provider_output([row(5)]).replace(b'"stop"', b'"content_filter"'),
            provider_output([row(5, amount=-1234)]),
            provider_output([row(0)]),
            provider_output([row(20)]),
            provider_output([row(5, unexpected="Injected")]),
            b"invalid json",
            b"s" * (MAX_RESPONSE_BYTES + 1),
        ]
        for payload in payloads:
            with self.subTest(payload=payload[:60]):
                response = Response(payload)
                self.connection.getresponse.return_value = response
                with self.assertRaises(ApiError) as problem:
                    extract_statement(STATEMENT, api_key=KEY)
                self.assertEqual(problem.exception.status, 502)
                self.assertEqual(response.limit, MAX_RESPONSE_BYTES + 1)

    def test_oversize_and_malformed_key_do_not_call_provider(self):
        for key, text, field in (
            ("bad\r\nInjected header:test", STATEMENT, "api_key"),
            (KEY, "x" * (MAX_TEXT_BYTES + 1), "file"),
            (KEY, " ",
             "file"),
        ):
            with self.subTest(field=field), self.assertRaises(ApiError) as problem:
                extract_statement(text, api_key=key)
            self.assertEqual(problem.exception.field, field)
        self.https.assert_not_called()

    def test_compacts_padded_columns_without_removing_bookings(self):
        padding = " " * (MAX_TEXT_BYTES // 2)
        text = f"Booking date{padding}Debit{padding}Credit\n2026-11-11{padding}12.34\n2026-11-12{padding}1000.07"
        rows = [row(2), row(3, "Employer", date="2026-11-12", amount="+1000.07")]
        result = self.extract(text, rows)
        self.assertEqual([item["amount"] for item in result["rows"]], ["-12.34", "1000.07"])
        body = json.loads(self.connection.request.call_args.kwargs["body"])
        self.assertIn("<5000sp>", body["messages"][1]["content"])
        self.assertIn("original horizontal spaces", body["messages"][0]["content"])

    def test_http_errors_safe_and_redirect_never_retransmits_bearer(self):
        for code in (401, 403, 429, 301, 307, 500):
            with self.subTest(code=code), self.assertRaises(ApiError) as problem:
                response = Response(b"Groq diagnostic with private text " + KEY.encode(), status=code)
                self.connection.getresponse.return_value = response
                self.connection.request.reset_mock()
                extract_statement(STATEMENT, api_key=KEY)
            self.assertEqual(problem.exception.status, 429 if code == 429 else 422 if code in (401, 403) else 502)
            self.assertNotIn(KEY, str(problem.exception))
            self.assertNotIn("private text", str(problem.exception))
            self.connection.request.assert_called_once()
            self.assertIsNone(response.limit)

    def test_connection_failure_closes_socket_and_no_secret_appears(self):
        self.connection.request.side_effect = TimeoutError("private transport diagnostic")
        with self.assertRaises(ApiError) as problem:
            extract_statement(STATEMENT, api_key=KEY)
        self.assertNotIn("private transport", str(problem.exception))
        self.connection.close.assert_called_once()

    def test_environment_key_and_empty_results(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(configured())
        with patch.dict(os.environ, {"GROQ_API_KEY": KEY}):
            self.assertTrue(configured())
            self.connection.getresponse.return_value = Response(provider_output([]))
            with self.assertRaisesRegex(ApiError, "no transaction rows"):
                extract_statement("Bank statement without bookings")
