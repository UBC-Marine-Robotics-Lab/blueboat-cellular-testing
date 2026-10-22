"""Regression coverage for serving-cell extraction without a connected modem."""

import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock
import urllib.error
import urllib.parse


SPEC = importlib.util.spec_from_file_location(
    "cell_modem_diagnostics",
    Path(__file__).with_name("cell-modem-diagnostics.py"),
)
diagnostics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostics)

API_BASE = "http://127.0.0.1:32769/v1.0"
FDD_RESPONSE = (
    '+QENG: "servingcell","NOCONN","LTE","FDD",302,220,'
    '001234AB,123,1650,4,5,5,00AF,-97,-11,-65,0,20\r\n\r\nOK\r\n'
)
EXPECTED_LTE = {
    "rat": "LTE",
    "cell_id": int("001234AB", 16),
    "pcid": 123,
    "earfcn": 1650,
    "lte_band": 4,
    "rsrp_dbm": -97,
    "rsrq_db": -11,
    "rssi_dbm": -65,
    "sinr_db": 0,
}


class ReadCellMetricsTests(unittest.TestCase):
    def test_actual_api_fields_map_lte_signals_and_preserve_zero(self):
        payload = {
            "serving_cell": {
                "rat": "LTE",
                "cell_id": 1193131,
                "signal_quality_dbm": -97,
                "signal_inr_db": 0,
            },
            "neighbor_cells": [],
        }
        with mock.patch.object(
            diagnostics,
            "request_json",
            side_effect=[payload, RuntimeError("commander unavailable")],
        ):
            metrics = diagnostics.read_cell_metrics(API_BASE, "modem")

        self.assertEqual(metrics["rat"], "LTE")
        self.assertEqual(metrics["cell_id"], 1193131)
        self.assertEqual(metrics["rsrp_dbm"], -97)
        self.assertEqual(metrics["sinr_db"], 0)

    def test_neighbour_cells_cannot_fill_missing_serving_metrics(self):
        payload = {
            "neighbor_cells": [{"rat": "LTE", **EXPECTED_LTE}],
            "serving_cell": {"rat": "LTE", "signal_quality_dbm": -109},
        }
        with mock.patch.object(
            diagnostics,
            "request_json",
            side_effect=[payload, RuntimeError("commander unavailable")],
        ):
            metrics = diagnostics.read_cell_metrics(API_BASE, "modem")

        self.assertEqual(metrics["rsrp_dbm"], -109)
        for field in ("cell_id", "pcid", "earfcn", "lte_band", "sinr_db"):
            with self.subTest(field=field):
                self.assertIsNone(metrics[field])

    def test_absent_serving_cell_does_not_select_a_neighbour(self):
        payloads = (
            {"serving_cell": None, "neighbor_cells": [EXPECTED_LTE]},
            {"serving_cell": {}, "neighbor_cells": [EXPECTED_LTE]},
            {"neighbor_cells": [EXPECTED_LTE]},
            {"neighbour_cells": [EXPECTED_LTE]},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with mock.patch.object(
                    diagnostics, "request_json", return_value=payload
                ) as request:
                    metrics = diagnostics.read_cell_metrics(API_BASE, "modem")
                for field in EXPECTED_LTE:
                    self.assertIsNone(metrics[field], field)
                request.assert_called_once()

    def test_non_lte_generic_signals_are_not_reported_as_lte_metrics(self):
        payload = {
            "serving_cell": {
                "rat": "WCDMA",
                "cell_id": 42,
                "signal_quality_dbm": -75,
                "signal_inr_db": 12,
            }
        }
        with mock.patch.object(
            diagnostics, "request_json", return_value=payload
        ) as request:
            metrics = diagnostics.read_cell_metrics(API_BASE, "modem")

        self.assertEqual(metrics["rat"], "WCDMA")
        self.assertEqual(metrics["cell_id"], 42)
        self.assertIsNone(metrics["rsrp_dbm"])
        self.assertIsNone(metrics["sinr_db"])
        request.assert_called_once()

    def test_complete_explicit_metrics_keep_existing_aliases(self):
        payload = {
            "serving_cell": {
                "technology": "LTE",
                "cell-id": 1193131,
                "physical_cell_id": 123,
                "earfcn": 1650,
                "band": 4,
                "RSRP": -97,
                "RSRQ": -11,
                "RSSI": -65,
                "SNR": 0,
            }
        }
        with mock.patch.object(
            diagnostics, "request_json", return_value=payload
        ) as request:
            metrics = diagnostics.read_cell_metrics(API_BASE, "modem")

        for field, expected in EXPECTED_LTE.items():
            self.assertEqual(metrics[field], expected, field)
        self.assertEqual(json.loads(metrics["cell_json"]), payload)
        request.assert_called_once()

    def test_fallback_posts_encoded_command_and_retains_raw_response(self):
        payload = {"serving_cell": {"rat": "LTE", "signal_quality_dbm": -110}}
        modem_id = "usb modem/1?port=at"
        with mock.patch.object(
            diagnostics, "request_json", side_effect=[payload, FDD_RESPONSE]
        ) as request:
            metrics = diagnostics.read_cell_metrics(API_BASE, modem_id)

        for field, expected in EXPECTED_LTE.items():
            self.assertEqual(metrics[field], expected, field)
        self.assertEqual(request.call_count, 2)
        call = request.call_args_list[1]
        self.assertEqual(call.kwargs, {"method": "POST"})
        parsed_url = urllib.parse.urlsplit(call.args[0])
        self.assertEqual(
            parsed_url.path,
            "/v1.0/modem/usb%20modem%2F1%3Fport%3Dat/commander",
        )
        self.assertEqual(
            urllib.parse.parse_qs(parsed_url.query),
            {"command": ['AT+QENG="servingcell"']},
        )
        retained = json.loads(metrics["cell_json"])
        self.assertEqual(retained["serving_cell"], payload["serving_cell"])
        self.assertEqual(retained["qeng_raw"], FDD_RESPONSE)
        self.assertFalse(metrics.get("error"))

    def test_fallback_http_failure_keeps_partial_api_measurements(self):
        payload = {
            "serving_cell": {
                "rat": "LTE",
                "signal_quality_dbm": -103,
                "signal_inr_db": 0,
            }
        }
        failure = urllib.error.HTTPError(
            API_BASE, 503, "Service Unavailable", None, None
        )
        with mock.patch.object(
            diagnostics, "request_json", side_effect=[payload, failure]
        ):
            metrics = diagnostics.read_cell_metrics(API_BASE, "modem")

        self.assertEqual(metrics["rsrp_dbm"], -103)
        self.assertEqual(metrics["sinr_db"], 0)
        self.assertIsNone(metrics["rsrq_db"])
        self.assertIn("503", metrics["error"])
        retained = json.loads(metrics["cell_json"])
        self.assertEqual(retained["serving_cell"], payload["serving_cell"])
        self.assertIn("503", retained["qeng_error"])

    def test_fallback_parse_failure_retains_raw_response_and_api_values(self):
        payload = {"serving_cell": {"rat": "LTE", "signal_quality_dbm": -103}}
        raw = "\r\n+CME ERROR: operation not supported\r\n"
        with mock.patch.object(
            diagnostics, "request_json", side_effect=[payload, raw]
        ):
            metrics = diagnostics.read_cell_metrics(API_BASE, "modem")

        self.assertEqual(metrics["rsrp_dbm"], -103)
        self.assertTrue(metrics["error"])
        retained = json.loads(metrics["cell_json"])
        self.assertEqual(retained["qeng_raw"], raw)
        self.assertTrue(retained["qeng_error"])


class QuectelServingCellParserTests(unittest.TestCase):
    def test_full_lte_fdd_response(self):
        metrics = diagnostics.parse_quectel_lte_serving_cell(FDD_RESPONSE)
        for field, expected in EXPECTED_LTE.items():
            self.assertEqual(metrics[field], expected, field)

    def test_lte_tdd_response_with_optional_srxlev_absent(self):
        raw = (
            '+QENG: "servingcell","CONNECT","LTE","TDD",302,220,'
            '001234AB,123,1650,4,5,5,00AF,-97,-11,-65,0\r\nOK\r\n'
        )
        metrics = diagnostics.parse_quectel_lte_serving_cell(raw)
        for field, expected in EXPECTED_LTE.items():
            self.assertEqual(metrics[field], expected, field)

    def test_unavailable_signal_tokens_are_none(self):
        raw = (
            '+QENG: "servingcell","NOCONN","LTE","FDD",302,220,'
            '001234AB,123,1650,4,5,5,00AF,-,,"",-,20\r\nOK\r\n'
        )
        metrics = diagnostics.parse_quectel_lte_serving_cell(raw)
        self.assertEqual(metrics["cell_id"], EXPECTED_LTE["cell_id"])
        for field in ("rsrp_dbm", "rsrq_db", "rssi_dbm", "sinr_db"):
            self.assertIsNone(metrics[field], field)

    def test_zero_cell_id_pcid_and_sinr_are_valid(self):
        raw = (
            '+QENG: "servingcell","NOCONN","LTE","FDD",302,220,'
            '00000000,0,1650,4,5,5,00AF,-97,-11,-65,0,20\r\nOK\r\n'
        )
        metrics = diagnostics.parse_quectel_lte_serving_cell(raw)
        for field in ("cell_id", "pcid", "sinr_db"):
            self.assertEqual(metrics[field], 0, field)

    def test_echo_and_neighbour_lines_do_not_replace_serving_cell(self):
        raw = (
            'AT+QENG="servingcell"\r\n'
            '+QENG: "neighbourcell intra","LTE",1650,456,-8,-70,-50,15\r\n'
            + FDD_RESPONSE
        )
        metrics = diagnostics.parse_quectel_lte_serving_cell(raw)
        self.assertEqual(metrics["pcid"], 123)
        self.assertEqual(metrics["rsrp_dbm"], -97)

    def test_rejects_responses_without_usable_lte_serving_cell(self):
        responses = {
            "empty": "",
            "error": "\r\nERROR\r\n",
            "cme_error": "\r\n+CME ERROR: 3\r\n",
            "no_service": '+QENG: "servingcell","SEARCH"\r\nOK\r\n',
            "wcdma": '+QENG: "servingcell","NOCONN","WCDMA",302,220,1,2,-75\r\n',
            "truncated": '+QENG: "servingcell","NOCONN","LTE","FDD",302,220,001234AB\r\n',
            "invalid_cell_id": FDD_RESPONSE.replace("001234AB", "not-hex"),
            "invalid_signal": FDD_RESPONSE.replace("-97", "not-a-number"),
            "neighbour_only": '+QENG: "neighbourcell intra","LTE",1650,456,-8,-70,-50,15\r\n',
        }
        for label, raw in responses.items():
            with self.subTest(response=label):
                with self.assertRaises(ValueError):
                    diagnostics.parse_quectel_lte_serving_cell(raw)


class RequestJsonTests(unittest.TestCase):
    def test_post_method_decodes_json_string_response(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            FDD_RESPONSE
        ).encode("utf-8")
        with mock.patch.object(
            diagnostics.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            result = diagnostics.request_json(API_BASE + "/commander", method="POST")

        self.assertEqual(result, FDD_RESPONSE)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, API_BASE + "/commander")


if __name__ == "__main__":
    unittest.main()
