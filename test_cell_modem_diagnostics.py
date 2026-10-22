"""Regression coverage for modem monitoring without connected hardware."""

from contextlib import ExitStack
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
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
        self.addCleanup(failure.close)
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


class ModemMonitorTests(unittest.TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.modem = {
            "id": "quectel-original",
            "manufacturer": "Quectel",
            "product": "EG25-G",
        }
        self.unrelated = {
            "id": "other-modem",
            "manufacturer": "Other",
            "product": "Other LTE modem",
        }
        self.now = 100.0
        self.radio = diagnostics.empty_cell_metrics()
        self.radio.update(EXPECTED_LTE, error=None)
        self.discovery = self.contexts.enter_context(
            mock.patch.object(
                diagnostics, "discover_modem_api_base", return_value=API_BASE
            )
        )
        self.inventory = self.contexts.enter_context(
            mock.patch.object(
                diagnostics, "request_json", return_value=[self.modem]
            )
        )
        self.read_radio = self.contexts.enter_context(
            mock.patch.object(
                diagnostics, "read_cell_metrics", return_value=self.radio
            )
        )
        self.contexts.enter_context(
            mock.patch.object(
                diagnostics.time, "monotonic", side_effect=lambda: self.now
            )
        )
        self.monitor = diagnostics.ModemMonitor()

    def test_present_inventory_is_checked_each_poll_and_api_is_cached(self):
        first = self.monitor.poll()
        second = self.monitor.poll()

        for sample in (first, second):
            self.assertIs(sample["modem_present"], True)
            self.assertEqual(sample["modem_id"], self.modem["id"])
            self.assertEqual(sample["radio"], self.radio)
            self.assertEqual(sample["modem_disconnect_count"], 0)
            self.assertEqual(sample["modem_event"], "")
            checked = diagnostics.datetime.fromisoformat(sample["modem_checked_utc"])
            self.assertIsNotNone(checked.utcoffset())
        self.discovery.assert_called_once_with()
        self.assertEqual(
            self.inventory.call_args_list,
            [mock.call(API_BASE + "/modem/")] * 2,
        )

    def test_disconnect_is_counted_once_and_reconnection_records_outage(self):
        self.monitor.poll()
        self.now = 110.0
        self.inventory.return_value = []
        disconnected = self.monitor.poll()
        self.now = 120.0
        missing = self.monitor.poll()
        self.now = 135.5
        self.inventory.return_value = [self.modem]
        reconnected = self.monitor.poll()

        self.assertIs(disconnected["modem_present"], False)
        self.assertEqual(disconnected["modem_event"], "disconnected")
        self.assertEqual(disconnected["modem_disconnect_count"], 1)
        self.assertEqual(missing["modem_event"], "")
        self.assertEqual(missing["modem_disconnect_count"], 1)
        self.assertIs(reconnected["modem_present"], True)
        self.assertEqual(reconnected["modem_event"], "reconnected")
        self.assertEqual(reconnected["modem_disconnect_count"], 1)
        self.assertAlmostEqual(reconnected["modem_outage_s"], 25.5)
        self.assertEqual(self.read_radio.call_count, 2)

    def test_startup_without_modem_keeps_polling_without_counting_disconnect(self):
        self.inventory.return_value = []
        absent = self.monitor.poll()
        still_absent = self.monitor.poll()
        self.read_radio.assert_not_called()
        self.inventory.return_value = [self.modem]
        appeared = self.monitor.poll()

        self.assertIs(absent["modem_present"], False)
        self.assertEqual(absent["modem_event"], "not_detected")
        self.assertEqual(absent["modem_disconnect_count"], 0)
        self.assertEqual(still_absent["modem_event"], "")
        self.assertIs(appeared["modem_present"], True)
        self.assertEqual(appeared["modem_disconnect_count"], 0)
        self.read_radio.assert_called_once_with(API_BASE, self.modem["id"])

    def test_inventory_outage_is_unknown_and_does_not_create_disconnect(self):
        self.monitor.poll()
        self.inventory.side_effect = RuntimeError("API unavailable")
        unknown = self.monitor.poll()
        self.inventory.side_effect = None
        recovered = self.monitor.poll()

        self.assertIsNone(unknown["modem_present"])
        self.assertEqual(unknown["modem_event"], "")
        self.assertEqual(unknown["modem_disconnect_count"], 0)
        self.assertIn("API unavailable", unknown["radio"]["error"])
        self.assertIs(recovered["modem_present"], True)
        self.assertEqual(recovered["modem_event"], "")
        self.assertEqual(recovered["modem_disconnect_count"], 0)

    def test_unknown_gap_preserves_confirmed_disconnect_and_outage_duration(self):
        self.monitor.poll()
        self.now = 110.0
        self.inventory.return_value = []
        self.monitor.poll()
        self.now = 120.0
        self.inventory.side_effect = RuntimeError("API unavailable")
        unknown = self.monitor.poll()
        self.now = 140.0
        self.inventory.side_effect = None
        self.inventory.return_value = [self.modem]
        reconnected = self.monitor.poll()

        self.assertIsNone(unknown["modem_present"])
        self.assertEqual(unknown["modem_disconnect_count"], 1)
        self.assertEqual(reconnected["modem_event"], "reconnected")
        self.assertEqual(reconnected["modem_disconnect_count"], 1)
        self.assertAlmostEqual(reconnected["modem_outage_s"], 30.0)

    def test_failed_inventory_retries_discovery_and_accepts_new_port(self):
        self.monitor.poll()
        new_base = "http://127.0.0.1:40001/v1.0"
        self.discovery.return_value = new_base
        self.inventory.side_effect = [RuntimeError("old port closed"), [self.modem]]
        recovered = self.monitor.poll()

        self.assertIs(recovered["modem_present"], True)
        self.assertEqual(recovered["modem_event"], "")
        self.assertEqual(self.discovery.call_count, 2)
        self.assertEqual(
            self.inventory.call_args_list[-2:],
            [mock.call(API_BASE + "/modem/"), mock.call(new_base + "/modem/")],
        )
        self.read_radio.assert_called_with(new_base, self.modem["id"])

    def test_initial_discovery_failure_is_unknown_and_later_recovers(self):
        self.discovery.side_effect = RuntimeError("container unavailable")
        unknown = self.monitor.poll()
        self.discovery.side_effect = None
        present = self.monitor.poll()

        self.assertIsNone(unknown["modem_present"])
        self.assertEqual(unknown["modem_disconnect_count"], 0)
        self.assertIs(present["modem_present"], True)
        self.assertEqual(present["modem_disconnect_count"], 0)

    def test_radio_failure_does_not_mean_modem_was_disconnected(self):
        self.read_radio.side_effect = RuntimeError("radio query timed out")
        sample = self.monitor.poll()

        self.assertIs(sample["modem_present"], True)
        self.assertEqual(sample["modem_disconnect_count"], 0)
        self.assertEqual(sample["modem_event"], "")
        self.assertIn("radio query timed out", sample["radio"]["error"])

    def test_prefers_eg25_and_keeps_original_id_after_inventory_reordering(self):
        replacement = dict(self.modem, id="another-eg25")
        self.inventory.return_value = [self.unrelated, self.modem]
        selected = self.monitor.poll()
        self.inventory.return_value = [replacement, self.unrelated, self.modem]
        reordered = self.monitor.poll()

        self.assertEqual(selected["modem_id"], self.modem["id"])
        self.assertEqual(reordered["modem_id"], self.modem["id"])
        self.assertEqual(reordered["modem_event"], "")
        self.assertEqual(reordered["modem_disconnect_count"], 0)

    def test_unrelated_modem_cannot_hide_tracked_modem_disconnection(self):
        self.monitor.poll()
        self.inventory.return_value = [self.unrelated]
        missing = self.monitor.poll()

        self.assertIs(missing["modem_present"], False)
        self.assertEqual(missing["modem_event"], "disconnected")
        self.assertEqual(missing["modem_disconnect_count"], 1)
        self.assertEqual(self.read_radio.call_count, 1)

    def test_known_second_modem_cannot_hide_disconnect_or_take_reconnection(self):
        other_eg25 = dict(self.modem, id="second-eg25")
        replacement = dict(self.modem, id="reenumerated-eg25")
        self.inventory.return_value = [self.modem, other_eg25]
        selected = self.monitor.poll()
        self.now = 110.0
        self.inventory.return_value = [other_eg25]
        missing = self.monitor.poll()
        self.now = 120.0
        self.inventory.return_value = [other_eg25, replacement]
        reconnected = self.monitor.poll()

        self.assertEqual(selected["modem_id"], self.modem["id"])
        self.assertIs(missing["modem_present"], False)
        self.assertEqual(missing["modem_event"], "disconnected")
        self.assertEqual(missing["modem_disconnect_count"], 1)
        self.assertIs(reconnected["modem_present"], True)
        self.assertEqual(reconnected["modem_id"], replacement["id"])
        self.assertEqual(reconnected["modem_event"], "reconnected")
        self.assertEqual(reconnected["modem_disconnect_count"], 1)
        self.assertAlmostEqual(reconnected["modem_outage_s"], 10.0)
        self.assertEqual(
            self.read_radio.call_args_list,
            [
                mock.call(API_BASE, self.modem["id"]),
                mock.call(API_BASE, replacement["id"]),
            ],
        )

    def test_reused_id_with_different_product_or_manufacturer_is_missing(self):
        for changes in (
            {"product": "Different modem"},
            {"manufacturer": "Different manufacturer"},
        ):
            with self.subTest(changes=changes):
                monitor = diagnostics.ModemMonitor()
                self.inventory.return_value = [self.modem]
                monitor.poll()
                self.read_radio.reset_mock()
                self.inventory.return_value = [dict(self.modem, **changes)]
                missing = monitor.poll()

                self.assertIs(missing["modem_present"], False)
                self.assertEqual(missing["modem_event"], "disconnected")
                self.assertEqual(missing["modem_disconnect_count"], 1)
                self.read_radio.assert_not_called()

    def test_new_id_for_same_model_reports_change_or_reconnection(self):
        self.monitor.poll()
        replacement = dict(self.modem, id="quectel-new-id")
        self.inventory.return_value = [replacement]
        changed = self.monitor.poll()
        self.assertIs(changed["modem_present"], True)
        self.assertEqual(changed["modem_id"], replacement["id"])
        self.assertEqual(changed["modem_event"], "changed")
        self.assertEqual(changed["modem_disconnect_count"], 0)

        self.now = 110.0
        self.inventory.return_value = []
        self.monitor.poll()
        self.now = 120.0
        self.inventory.return_value = [self.modem]
        reconnected = self.monitor.poll()
        self.assertEqual(reconnected["modem_id"], self.modem["id"])
        self.assertEqual(reconnected["modem_event"], "reconnected")
        self.assertEqual(reconnected["modem_disconnect_count"], 1)
        self.assertAlmostEqual(reconnected["modem_outage_s"], 10.0)

    def test_malformed_inventory_is_unknown_instead_of_disconnect(self):
        self.monitor.poll()
        for invalid in (None, {"modems": []}, "bad response", [{}], [None]):
            with self.subTest(inventory=invalid):
                self.inventory.return_value = invalid
                sample = self.monitor.poll()
                self.assertIsNone(sample["modem_present"])
                self.assertEqual(sample["modem_event"], "")
                self.assertEqual(sample["modem_disconnect_count"], 0)
                self.assertTrue(sample["radio"]["error"])
        self.inventory.return_value = [self.modem]
        recovered = self.monitor.poll()
        self.assertIs(recovered["modem_present"], True)
        self.assertEqual(recovered["modem_event"], "")

    def test_main_writes_csv_through_startup_absence_and_reconnections(self):
        log_directory = Path(
            self.contexts.enter_context(tempfile.TemporaryDirectory())
        )
        self.contexts.enter_context(
            mock.patch.object(diagnostics, "LOG_DIRECTORY", log_directory)
        )
        self.contexts.enter_context(
            mock.patch.object(
                diagnostics,
                "ping_stats",
                return_value={"reachable": True, "loss": 0, "avg": 12, "mdev": 1},
            )
        )
        self.contexts.enter_context(
            mock.patch.object(diagnostics, "route_interface", return_value="usb1")
        )
        terminal = self.contexts.enter_context(mock.patch("builtins.print"))
        self.inventory.side_effect = [[], [self.modem], [], [self.modem]]
        cycles = 0

        def advance_or_stop(seconds):
            nonlocal cycles
            cycles += 1
            self.now += seconds
            if cycles == 4:
                raise KeyboardInterrupt

        self.contexts.enter_context(
            mock.patch.object(diagnostics.time, "sleep", side_effect=advance_or_stop)
        )
        with self.assertRaises(KeyboardInterrupt):
            diagnostics.main()

        logs = list(log_directory.glob("cell-link-log-*.csv"))
        self.assertEqual(len(logs), 1)
        with logs[0].open(newline="") as file:
            rows = list(csv.DictReader(file))

        self.assertEqual(
            [row["modem_event"] for row in rows],
            ["not_detected", "reconnected", "disconnected", "reconnected"],
        )
        self.assertEqual(
            [row["modem_present"] for row in rows],
            ["False", "True", "False", "True"],
        )
        self.assertEqual(
            [row["modem_disconnect_count"] for row in rows], ["0", "0", "1", "1"]
        )
        self.assertEqual(float(rows[-1]["modem_outage_s"]), diagnostics.LOG_PERIOD_S)
        for row in rows:
            self.assertTrue(row["modem_checked_utc"])
            self.assertEqual(row["consecutive_laptop_failures"], "0")
        self.assertTrue(rows[0]["error"])
        self.assertFalse(rows[-1]["error"])
        self.assertTrue(
            any("DISCONNECTED" in str(call) for call in terminal.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
