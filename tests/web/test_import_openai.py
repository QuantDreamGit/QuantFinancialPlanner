import base64
import json
import os
import unittest
from unittest.mock import patch

from apps.web import import_openai as openai
from apps.web.values import ApiError


PDF = b"%PDF-1.7\nsynthetic PDF\n%%EOF"
KEY = "sk-proj-synthetic-key-for-tests"


def row(**overrides):
    return {
        "date": "2026-11-16", "description": "CAFFÈ CORNER", "amount": "-8.75", "currency": "EUR",
        "reference": "BANK-001", "page": 2, "evidence": "16/11/2026 CAFFÈ CORNER -8,75", "issue": None,
        **overrides,
    }


def result(rows, **overrides):
    return {"rows": rows, "document_type": "statement", "total_debit": None, "total_credit": None,
            "document_note": None, **overrides}


def response(result=None, *, status="completed", content=None):
    return json.dumps({"status": status, "output": [{"role": "assistant", "type": "message", "status": "completed",
        "content": content or [{"type": "output_text", "text": json.dumps(result)}],
    }]}).encode()


class OpenAIPDFTests(unittest.TestCase):
    def extract(self, *rows, **overrides):
        with patch.object(openai, "_request", return_value=response(result(rows, **overrides))) as request:
            extracted = openai.extract_statement(PDF, api_key=KEY)
        request.assert_called_once_with(PDF, KEY, openai.MODEL)
        return extracted

    def test_request_posts_inline_pdf_with_strict_schema_and_no_tools(self):
        with patch.object(openai, "HTTPSConnection") as connection:
            network = connection.return_value
            network.getresponse.return_value.status = 200
            network.getresponse.return_value.read.return_value = response(result([row()]))
            extraction = openai.extract_statement(PDF, api_key=KEY)
        connection.assert_called_once_with("api.openai.com", timeout=openai.REQUEST_SECONDS)
        args, kwargs = network.request.call_args
        self.assertEqual(args, ("POST", "/v1/responses"))
        payload = json.loads(kwargs["body"])
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertNotIn(KEY, kwargs["body"].decode())
        self.assertFalse(payload["store"])
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["model"], "gpt-4.1-mini")
        document = payload["input"][0]["content"][0]
        self.assertEqual((document["type"], document["filename"]), ("input_file", "statement.pdf"))
        self.assertEqual(document["file_data"], "data:application/pdf;base64," + base64.b64encode(PDF).decode())
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertFalse(payload["text"]["format"]["schema"]["additionalProperties"])
        network.getresponse.return_value.read.assert_called_once_with(openai.MAX_RESPONSE_BYTES + 1)
        network.close.assert_called_once()
        self.assertEqual(extraction["rows"][0]["reference"], "BANK-001")

    def test_selected_model_and_reasoning_metadata_are_supported(self):
        envelope = json.loads(response(result([row()])))
        envelope["output"].insert(0, {"type": "reasoning", "summary": []})
        selected = "gpt-5.4"
        with patch.object(openai, "_send", return_value=json.dumps(envelope).encode()) as send:
            extraction = openai.extract_statement(PDF, api_key=KEY, model=selected)
            self.assertEqual(send.call_args.args[0]["model"], selected)
            self.assertEqual(extraction["rows"][0]["reference"], "BANK-001")
            with self.assertRaisesRegex(ApiError, "Choose one of the listed options"):
                openai.extract_statement(PDF, api_key=KEY, model="unknown")
            self.assertEqual(send.call_count, 1)

    def test_keeps_duplicate_bookings_and_normalizes_printed_totals_in_order(self):
        extracted = self.extract(row(), row(), row(amount="+30", page=3, reference="BANK-002"),
                                 total_debit="17.5", total_credit="30", document_note="Small print on page 4")
        self.assertEqual([item["page"] for item in extracted["rows"]], [2, 2, 3])
        self.assertEqual([item["amount"] for item in extracted["rows"]], ["-8.75", "-8.75", "30.00"])
        self.assertEqual(extracted["rows"][0], extracted["rows"][1])
        self.assertEqual((extracted["total_debit"], extracted["total_credit"], extracted["document_note"]),
                         ("17.50", "30.00", "Small print on page 4"))
        self.assertEqual(extracted["notices"], [])

    def test_missing_or_uncertain_fields_stay_reviewable(self):
        extracted = self.extract(
            row(date="2026-02-30", amount="8.75", currency="X", page=-1, evidence=None),
            row(amount="-92233720368547758.08", issue="Running balance needs checking", currency="USD"),
        )
        first, second = extracted["rows"]
        self.assertEqual((first["date"], first["amount"], first["currency"], first["page"], first["evidence"]),
                         ("", "", "", None, ""))
        self.assertEqual((second["amount"], second["currency"], second["issue"]),
                         ("", "USD", "Running balance needs checking"))
        self.assertTrue(first["issue"])
        self.assertEqual(len(extracted["notices"]), 1)

    def test_only_single_explicit_debit_is_kept_for_paid_invoice(self):
        extracted = self.extract(row(), document_type="paid_invoice", total_debit="8.75")
        self.assertEqual((extracted["rows"][0]["amount"], extracted["total_debit"]), ("-8.75", ""))
        for entries in ((row(), row()), (row(amount="+8.75"),)):
            with self.subTest(entries=entries):
                rows = self.extract(*entries, document_type="paid_invoice")["rows"]
                self.assertEqual(len(rows), len(entries))
                self.assertTrue(all(not item["amount"] and not item["date"] and item["issue"] for item in rows))

    def test_unpaid_invoice_can_never_return_a_booked_amount_or_date(self):
        for entries in ((), (row(),)):
            with self.subTest(entries=entries):
                rows = self.extract(*entries, document_type="unpaid_invoice")["rows"]
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]["amount"], rows[0]["date"]), ("", ""))
                self.assertIn("does not confirm a payment", rows[0]["issue"])

    def test_unknown_documents_and_empty_statements_are_flagged_for_review(self):
        unknown = self.extract(row(), document_type="unknown")
        self.assertTrue(unknown["rows"][0]["issue"])
        self.assertEqual(unknown["rows"][0]["evidence"], row()["evidence"])
        no_bookings = self.extract(total_debit="0.00", total_credit="0.00")
        self.assertEqual(no_bookings["rows"], [])
        self.assertIn("no booked transactions", no_bookings["notices"][0])

    def test_partial_refused_oversized_and_malformed_results_are_rejected(self):
        for body in (response(result([row()]), status="incomplete"),
                     response(content=[{"type": "refusal", "refusal": KEY}]),
                     response(result([row(page=True)])), response(result([row(account="unexpected")])),
                     response(result([row()] * (openai.MAX_ROWS + 1))),
                     response(content=[{"type": "output_text", "text": '{"rows": [], "rows": []}'}]),
                     b"truncated response"):
            with self.subTest(size=len(body)), patch.object(openai, "_request", return_value=body):
                with self.assertRaises(ApiError) as failure:
                    openai.extract_statement(PDF, api_key=KEY)
                self.assertNotIn(KEY, str(failure.exception))

    def test_transport_never_reads_redirects_or_error_bodies(self):
        for status, expected in ((302, 502), (401, 422), (413, 413), (429, 429)):
            with self.subTest(status=status), patch.object(openai, "HTTPSConnection") as connection:
                network = connection.return_value
                network.getresponse.return_value.status = status
                network.getresponse.return_value.headers = {"Location": "https://example.invalid"}
                with self.assertRaises(ApiError) as failure:
                    openai.extract_statement(PDF, api_key=KEY)
                self.assertEqual(failure.exception.status, expected)
                self.assertEqual(network.request.call_count, 1)
                network.getresponse.return_value.read.assert_not_called()
                network.close.assert_called_once()

    def test_enforces_input_and_response_limits_and_key_validation(self):
        with patch.object(openai, "_request") as request, patch.dict(os.environ, {"OPENAI_API_KEY": KEY}):
            self.assertTrue(openai.configured())
            self.assertEqual(openai.validate_api_key(f" {KEY} "), KEY)
            for binary, key in ((b"not a PDF", None), (PDF, "wrong\nheader"),
                                (PDF + b"0" * openai.MAX_PDF_BYTES, None)):
                with self.assertRaises(ApiError):
                    openai.extract_statement(binary, api_key=key)
            request.assert_not_called()
        with patch.object(openai, "HTTPSConnection") as connection:
            reply = connection.return_value.getresponse.return_value
            reply.status = 200
            reply.read.return_value = b"0" * (openai.MAX_RESPONSE_BYTES + 1)
            with self.assertRaisesRegex(ApiError, "too much data"):
                openai.extract_statement(PDF, api_key=KEY)

    def test_transport_failure_omits_request_secrets(self):
        with patch.object(openai, "HTTPSConnection") as connection:
            network = connection.return_value
            network.request.side_effect = TimeoutError(KEY)
            with self.assertRaises(ApiError) as failure:
                openai.extract_statement(PDF, api_key=KEY)
        network.close.assert_called_once()
        self.assertEqual(failure.exception.status, 502)
        self.assertNotIn(KEY, str(failure.exception))


class OpenAICSVTests(unittest.TestCase):
    def test_maps_numbered_columns_from_samples_with_a_strict_text_schema(self):
        headers = ["Booking", "Merchant", "Debited"]
        samples = [["17.11.2026", "Caffè Corner", "8,75"]]
        mapping = dict.fromkeys(openai.CSV_FIELDS) | {"date": 0, "name": 1, "amount": 2}
        layout = {"mapping": mapping, "date_format": "dmy", "amount_mode": "debit-positive"}
        with patch.object(openai, "_send", return_value=response(layout)) as send:
            result = openai.suggest_csv_mapping(headers, samples, api_key=KEY, model="gpt-5.4-mini")
        payload, key, subject = send.call_args.args
        self.assertEqual((key, subject), (KEY, "CSV"))
        self.assertEqual(payload["model"], "gpt-5.4-mini")
        self.assertEqual(json.loads(payload["input"]), {"headers": headers, "samples": samples})
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(result, dict(layout, mapping={"date": 0, "name": 1, "amount": 2}))

        for invalid in (True, -1, 3):
            bad_layout = dict(layout, mapping=dict(mapping, date=invalid))
            with self.subTest(invalid=invalid), patch.object(openai, "_send", return_value=response(bad_layout)):
                with self.assertRaisesRegex(ApiError, "invalid column layout"):
                    openai.suggest_csv_mapping(headers, samples, api_key=KEY)


class OpenAIReviewPromptTests(unittest.TestCase):
    def test_returns_only_changes_and_rejects_rows_outside_the_draft(self):
        draft = {"rows": [{"id": 1, "posted_on": "2026-11-17"}, {"id": 28, "posted_on": ""}]}
        update = dict.fromkeys(openai.EDIT_PROPERTIES) | {"rows": [28], "posted_on": "2026-11-16"}
        suggested = {"updates": [update], "sort": None, "message": None}
        instruction = "Use 16-11-2026 for missing dates."
        with patch.object(openai, "_send", return_value=response(suggested)) as send:
            result = openai.suggest_review_edits(instruction, draft, api_key=KEY, model="gpt-5.4-mini")
        payload, key, subject = send.call_args.args
        self.assertEqual((key, subject, payload["model"]), (KEY, "review table", "gpt-5.4-mini"))
        self.assertEqual(json.loads(payload["input"]), {"instruction": instruction, "table": draft})
        self.assertFalse(payload["store"])
        self.assertEqual(result["updates"], [{"rows": [28], "posted_on": "2026-11-16"}])
        with patch.object(openai, "_send", return_value=response(dict(suggested, updates=[dict(update, rows=[7])]))):
            with self.assertRaisesRegex(ApiError, "invalid table edits"):
                openai.suggest_review_edits(instruction, draft, api_key=KEY)


if __name__ == "__main__":
    unittest.main()
