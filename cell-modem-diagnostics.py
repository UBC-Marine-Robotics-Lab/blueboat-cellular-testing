#!/usr/bin/env python3

import csv
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request

from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# USER SETTINGS
# ============================================================

# IMPORTANT:
# Set this to your LAPTOP'S ZeroTier Managed IP.
#
# This is NOT:
#   - the BlueBoat's ZeroTier IP
#   - your laptop's Wi-Fi IP
#
# Example:
# LAPTOP_ZT_IP = "10.147.20.23"
#

EMMA_E14_ZT_IP = "10.41.236.148"
MAKAYLA_ZT_IP = "10.41.236.163"

LAPTOP_ZT_IP = EMMA_E14_ZT_IP


# Internal port used by the Blue Robotics
# Cellular Modem Manager Docker container.
MODEM_CONTAINER_PORT = 20038


# Public server used to measure:
#
# BlueBoat -> cellular network -> Internet
#
# Set to None if you do not want this measurement.
PUBLIC_PING_TARGET = "1.1.1.1"


# Number of ping packets per measurement
PING_COUNT = 3

# Ping timeout in seconds
PING_TIMEOUT_S = 2

# Time between logging cycles, including modem-presence checks.
# Disconnects entirely between checks may be missed.
LOG_PERIOD_S = 10


# Each run creates a CSV in ~/blueboat-cellular-logs.
# Filename: cell-link-log-YEAR-DAY-MONTH-HHMMSS.csv (24-hour UTC).
# The date and time come from the Pi's system clock.
LOG_DIRECTORY = Path.home() / "blueboat-cellular-logs"


# ============================================================
# CELLULAR MODEM MANAGER DISCOVERY
# ============================================================

def discover_modem_api_base():
    """
    Find the dynamically assigned BlueOS host port for the
    Cellular Modem Manager Docker container.

    Example Docker mapping:

        0.0.0.0:32769->20038/tcp

    Returns:

        http://127.0.0.1:32769/v1.0

    This means we do not have to hard-code 32769.
    """

    result = subprocess.run(
        [
            "docker",
            "ps",
            "--format",
            "{{.Names}}|{{.Image}}|{{.Ports}}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )

    for line in result.stdout.splitlines():

        line_lower = line.lower()

        # Match either the image name or BlueOS extension name.
        if (
            "cellphone-modem-manager" not in line_lower
            and "cellphonemodemmanager" not in line_lower
        ):
            continue

        # Look for:
        #
        # :32769->20038/tcp
        #
        match = re.search(
            rf":(\d+)->{MODEM_CONTAINER_PORT}/tcp",
            line,
        )

        if match:

            host_port = int(
                match.group(1)
            )

            return (
                f"http://127.0.0.1:"
                f"{host_port}/v1.0"
            )

    raise RuntimeError(
        "Could not find the Cellular Modem Manager "
        "Docker port mapping."
    )


# ============================================================
# HTTP HELPERS
# ============================================================

def request_json(url, *, method="GET"):
    """
    Perform an HTTP request and decode the JSON response.
    """

    request = urllib.request.Request(
        url,
        method=method,
    )

    with urllib.request.urlopen(
        request,
        timeout=5,
    ) as response:

        text = response.read().decode(
            "utf-8",
            errors="replace",
        )

    return json.loads(text)


# ============================================================
# MODEM DISCOVERY
# ============================================================

def select_modem(modems, previous_modem=None, excluded_ids=()):
    """Retain the selected modem, allowing its USB path/ID to change."""

    if previous_modem is not None:
        for modem in modems:
            if (
                modem["id"] == previous_modem["id"]
                and all(
                    not previous_modem.get(key)
                    or modem.get(key) == previous_modem[key]
                    for key in ("manufacturer", "product")
                )
            ):
                return modem

        # A re-enumerated EG25 can have a new ID. Do not switch to
        # unrelated hardware just because it remains connected.
        for modem in modems:
            if (
                modem["id"] not in excluded_ids
                and previous_modem.get("product")
                and modem.get("product") == previous_modem["product"]
                and modem.get("manufacturer") == previous_modem.get("manufacturer")
            ):
                return modem
        return None

    for modem in modems:
        if "EG25" in str(modem.get("product", "")).upper():
            return modem

    return modems[0] if modems else None


class ModemMonitor:
    """Track observed hardware presence independently of radio/API failures."""

    def __init__(self):
        self.api_base = None
        self.modem = None
        self.last_present = None
        self.disconnect_count = 0
        self.disconnected_at = None
        self.other_modem_ids = set()

    def poll(self):
        radio = empty_cell_metrics()
        present = None
        selected = None

        for attempt in range(2):
            try:
                if self.api_base is None or attempt:
                    self.api_base = discover_modem_api_base()

                modems = request_json(f"{self.api_base}/modem/")
                if (
                    not isinstance(modems, list)
                    or any(
                        not isinstance(modem, dict)
                        or not isinstance(modem.get("id"), str)
                        or not modem["id"]
                        for modem in modems
                    )
                ):
                    raise ValueError("Invalid modem inventory response.")

                selected = select_modem(
                    modems,
                    self.modem,
                    self.other_modem_ids,
                )
                # An identical modem that was already present must not
                # hide the disappearance of the one we were monitoring.
                self.other_modem_ids.update(
                    modem["id"]
                    for modem in modems
                    if selected is None or modem["id"] != selected["id"]
                )
                present = selected is not None
                break

            except Exception as error:
                self.api_base = None
                radio["error"] = (
                    "Cellular: modem presence unknown: "
                    f"{type(error).__name__}: {error}"
                )

        # These timestamps describe when presence was observed; they
        # cannot establish the exact time of a change between polls.
        checked_utc = datetime.now(timezone.utc).isoformat()
        checked_at = time.monotonic()
        event = ""
        outage_s = None

        if present is False:
            if self.last_present is not False:
                self.disconnected_at = checked_at
                if self.last_present is True:
                    self.disconnect_count += 1
                    event = "disconnected"
                else:
                    event = "not_detected"
            outage_s = round(checked_at - self.disconnected_at, 1)
            radio["error"] = "Cellular: monitored modem is not detected."

        elif present is True:
            if self.last_present is False:
                event = "reconnected"
                outage_s = round(checked_at - self.disconnected_at, 1)
                self.disconnected_at = None
            elif self.modem is not None and selected["id"] != self.modem["id"]:
                event = "changed"

            self.modem = selected
            try:
                radio = read_cell_metrics(self.api_base, selected["id"])
            except Exception as error:
                radio = empty_cell_metrics()
                radio["error"] = (
                    "Cellular: radio query failed while modem was detected: "
                    f"{type(error).__name__}: {error}"
                )

        # An unknown API result leaves the last confirmed presence and
        # outage start intact, without inventing hardware transitions.
        if present is not None:
            self.last_present = present

        return {
            "radio": radio,
            "modem_id": self.modem["id"] if self.modem else None,
            "modem_present": present,
            "modem_event": event,
            "modem_disconnect_count": self.disconnect_count,
            "modem_outage_s": outage_s,
            "modem_checked_utc": checked_utc,
        }


# ============================================================
# CELLULAR JSON PARSING
# ============================================================

def normalize_key(key):
    """
    Make differently formatted JSON keys easier to match.

    Examples:

        RSRP       -> rsrp
        rsrp-dbm   -> rsrp_dbm
        Cell ID    -> cell_id
    """

    return (
        str(key)
        .lower()
        .strip()
        .replace("-", "_")
        .replace(" ", "_")
    )


def find_value_recursive(obj, possible_keys):
    """
    Search arbitrarily nested dictionaries/lists for the first
    value with one of the requested key names.
    """

    wanted = {
        normalize_key(key)
        for key in possible_keys
    }

    def search(item):

        if isinstance(item, dict):

            # First search the current level.
            for key, value in item.items():

                if (
                    normalize_key(key) in wanted
                    and value is not None
                    and value != ""
                ):
                    return value

            # Then search children.
            for value in item.values():

                result = search(value)

                if result is not None:
                    return result

        elif isinstance(item, list):

            for value in item:

                result = search(value)

                if result is not None:
                    return result

        return None

    return search(obj)


# ============================================================
# CELLULAR MEASUREMENTS
# ============================================================

def parse_quectel_lte_serving_cell(response):
    """
    Extract LTE measurements from AT+QENG="servingcell".

    The modem manager's /cell response omits RSRQ, RSSI, PCI,
    EARFCN and band. Its /commander endpoint exposes the full
    Quectel response without opening the serial port ourselves.
    """

    if not isinstance(response, str):
        raise ValueError("Expected a raw Quectel response string.")

    for line in response.splitlines():

        match = re.match(r"\s*\+QENG:\s*(.*)", line)

        if not match:
            continue

        fields = next(csv.reader(
            [match.group(1)],
            skipinitialspace=True,
        ))

        if (
            len(fields) < 3
            or fields[0].strip().lower() != "servingcell"
            or fields[2].strip().upper() != "LTE"
        ):
            continue

        if len(fields) < 17:
            raise ValueError("Incomplete LTE serving-cell response.")

        def number(index, base=10):
            value = fields[index].strip()
            if value in ("", "-"):
                return None
            return int(value, base)

        # Field order is defined by Quectel's QENG LTE format.
        # Cell ID is hexadecimal, even when it contains only digits.
        # Signal measurements are already in dBm/dB; SINR 0 is valid.
        return {
            "rat": "LTE",
            "cell_id": number(6, base=16),
            "pcid": number(7),
            "earfcn": number(8),
            "lte_band": number(9),
            "rsrp_dbm": number(13),
            "rsrq_db": number(14),
            "rssi_dbm": number(15),
            "sinr_db": number(16),
        }

    raise ValueError("No LTE serving-cell measurements in Quectel response.")


def read_cell_metrics(api_base, modem_id):
    """
    Ask Blue Robotics Cellular Modem Manager for its parsed
    serving-cell information.

    Internally, the modem manager obtains this information from
    the Quectel modem using AT+QENG="servingcell".
    """

    modem_id_encoded = urllib.parse.quote(
        modem_id,
        safe="",
    )

    url = (
        f"{api_base}/modem/"
        f"{modem_id_encoded}/cell"
    )

    data = request_json(url)

    # Never substitute neighboring-cell measurements for the serving cell.
    serving_cell = data
    if isinstance(data, dict):
        if "serving_cell" in data:
            serving_cell = data["serving_cell"]
        else:
            serving_cell = {
                key: value
                for key, value in data.items()
                if normalize_key(key) not in (
                    "neighbor_cells", "neighbour_cells",
                )
            }

    metrics = {

        # Radio access technology:
        # LTE, WCDMA, etc.
        "rat": find_value_recursive(
            serving_cell,
            [
                "rat",
                "access_technology",
                "technology",
            ],
        ),

        "cell_id": find_value_recursive(
            serving_cell,
            [
                "cell_id",
                "cellid",
                "cell-id",
            ],
        ),

        # Physical Cell ID
        "pcid": find_value_recursive(
            serving_cell,
            [
                "pcid",
                "pci",
                "physical_cell_id",
            ],
        ),

        # LTE channel number
        "earfcn": find_value_recursive(
            serving_cell,
            [
                "earfcn",
            ],
        ),

        "lte_band": find_value_recursive(
            serving_cell,
            [
                "band",
                "lte_band",
                "freq_band_ind",
            ],
        ),

        # Main signal-strength metric
        "rsrp_dbm": find_value_recursive(
            serving_cell,
            [
                "rsrp",
                "rsrp_dbm",
            ],
        ),

        # Signal quality
        "rsrq_db": find_value_recursive(
            serving_cell,
            [
                "rsrq",
                "rsrq_db",
            ],
        ),

        # Total received power
        "rssi_dbm": find_value_recursive(
            serving_cell,
            [
                "rssi",
                "rssi_dbm",
            ],
        ),

        # Signal-to-interference/noise metric
        "sinr_db": find_value_recursive(
            serving_cell,
            [
                "sinr",
                "snr",
                "sinr_db",
                "snr_db",
            ],
        ),

        # Store complete API response as well.
        #
        # This is useful during early testing because if BlueOS
        # uses slightly different field names, we still retain
        # all of the original information.
        "cell_json": json.dumps(
            data,
            separators=(",", ":"),
        ),

        "error": None,
    }

    if str(metrics["rat"]).upper() == "LTE":

        # These generic API fields mean RSRP and SINR for LTE.
        # Other technologies use them for different measurements.
        for metric, api_key in (
            ("rsrp_dbm", "signal_quality_dbm"),
            ("sinr_db", "signal_inr_db"),
        ):
            if metrics[metric] is None:
                metrics[metric] = find_value_recursive(
                    serving_cell,
                    [api_key],
                )

        if any(
            metrics[key] is None
            for key in (
                "cell_id", "pcid", "earfcn", "lte_band",
                "rsrp_dbm", "rsrq_db", "rssi_dbm", "sinr_db",
            )
        ):

            raw_data = (
                dict(data)
                if isinstance(data, dict)
                else {"cell": data}
            )

            try:
                query = urllib.parse.urlencode({
                    "command": 'AT+QENG="servingcell"',
                })
                response = request_json(
                    f"{api_base}/modem/{modem_id_encoded}/commander?{query}",
                    method="POST",
                )
                raw_data["qeng_raw"] = response
                # Keep the complete raw observation together if the
                # serving cell changed since the /cell request.
                metrics.update(parse_quectel_lte_serving_cell(response))

            except Exception as error:
                # Retain any measurements already supplied by /cell.
                raw_data["qeng_error"] = f"{type(error).__name__}: {error}"
                metrics["error"] = (
                    "Cellular: LTE detail query failed: "
                    f"{raw_data['qeng_error']}"
                )

            metrics["cell_json"] = json.dumps(
                raw_data,
                separators=(",", ":"),
            )

    return metrics


def empty_cell_metrics():
    """
    Blank cellular values used if the modem query fails.
    """

    return {
        "rat": None,
        "cell_id": None,
        "pcid": None,
        "earfcn": None,
        "lte_band": None,
        "rsrp_dbm": None,
        "rsrq_db": None,
        "rssi_dbm": None,
        "sinr_db": None,
        "cell_json": None,
        "error": None,
    }


# ============================================================
# PING MEASUREMENTS
# ============================================================

def ping_stats(target):
    """
    Ping a target and extract:

        reachable
        packet loss
        average RTT
        RTT mdev

    RTT mdev is useful as a measure of how variable the
    connection latency is.
    """

    if not target:

        return {
            "reachable": None,
            "loss": None,
            "avg": None,
            "mdev": None,
        }

    command = [
        "ping",
        "-n",
        "-c",
        str(PING_COUNT),
        "-W",
        str(PING_TIMEOUT_S),
        target,
    ]

    try:

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=(
                PING_COUNT
                * PING_TIMEOUT_S
                + 5
            ),
            check=False,
        )

        output = (
            (result.stdout or "")
            + "\n"
            + (result.stderr or "")
        )

        # Typical Linux output:
        #
        # 3 packets transmitted, 3 received,
        # 0% packet loss
        #
        loss_match = re.search(
            r"(\d+)\s+packets transmitted,\s+"
            r"(\d+)\s+(?:packets )?received,"
            r".*?([\d.]+)%\s+packet loss",
            output,
            re.S,
        )

        if loss_match:

            received = int(
                loss_match.group(2)
            )

            loss = float(
                loss_match.group(3)
            )

        else:

            received = 0
            loss = 100.0

        # Typical Linux output:
        #
        # rtt min/avg/max/mdev =
        # 40.1/45.2/50.3/3.2 ms
        #
        rtt_match = re.search(
            r"(?:rtt|round-trip).*?=\s*"
            r"[\d.]+/"
            r"([\d.]+)/"
            r"[\d.]+/"
            r"([\d.]+)\s*ms",
            output,
        )

        if rtt_match:

            avg = float(
                rtt_match.group(1)
            )

            mdev = float(
                rtt_match.group(2)
            )

        else:

            avg = None
            mdev = None

        return {
            "reachable": received > 0,
            "loss": loss,
            "avg": avg,
            "mdev": mdev,
        }

    except Exception:

        return {
            "reachable": False,
            "loss": 100.0,
            "avg": None,
            "mdev": None,
        }


# ============================================================
# ROUTING INFORMATION
# ============================================================

def route_interface(target):
    """
    Determine which Linux interface the BlueBoat would use
    to reach a target.

    This is useful for checking whether the Internet connection
    is actually leaving through cellular instead of Wi-Fi.
    """

    if not target:
        return None

    try:

        result = subprocess.run(
            [
                "ip",
                "route",
                "get",
                target,
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )

        match = re.search(
            r"\bdev\s+(\S+)",
            result.stdout,
        )

        if match:
            return match.group(1)

    except Exception:
        pass

    return None


# ============================================================
# CSV CONFIGURATION
# ============================================================

CSV_FIELDS = [

    "timestamp_utc",
    "elapsed_s",

    # Hardware presence, observed changes, and duration since absence
    "modem_id",
    "modem_present",
    "modem_event",
    "modem_disconnect_count",
    "modem_outage_s",
    "modem_checked_utc",

    # Cellular radio measurements
    "rat",
    "cell_id",
    "pcid",
    "earfcn",
    "lte_band",

    "rsrp_dbm",
    "rsrq_db",
    "rssi_dbm",
    "sinr_db",

    # BlueBoat -> ZeroTier -> laptop
    "laptop_reachable",
    "laptop_loss_pct",
    "laptop_rtt_avg_ms",
    "laptop_rtt_mdev_ms",
    "laptop_route_if",

    # BlueBoat -> Internet
    "internet_reachable",
    "internet_loss_pct",
    "internet_rtt_avg_ms",
    "internet_rtt_mdev_ms",
    "internet_egress_if",

    # Dropout tracking
    "consecutive_laptop_failures",

    # Complete modem-manager response
    "cell_json",

    # Any error encountered during this cycle
    "error",
]


# ============================================================
# MAIN LOGGER
# ============================================================

def create_log_file():
    """Create a separate CSV for this run without replacing existing logs."""

    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%d-%m-%H%M%S")
    stem = f"cell-link-log-{timestamp}"
    attempt = 0

    while True:
        suffix = f"-{attempt}" if attempt else ""
        log_file = LOG_DIRECTORY / f"{stem}{suffix}.csv"

        try:
            # Exclusive creation also keeps simultaneous runs separate.
            return log_file.open("x", newline="", buffering=1)
        except FileExistsError:
            attempt += 1


def main():

    # --------------------------------------------------------
    # Check settings
    # --------------------------------------------------------

    if LAPTOP_ZT_IP == "CHANGE_ME":

        raise SystemExit(
            "\n"
            "ERROR: You need to set LAPTOP_ZT_IP at the "
            "top of this file to your laptop's ZeroTier "
            "Managed IP.\n"
        )

    # --------------------------------------------------------
    # Start monitoring, including when the modem is initially absent
    # --------------------------------------------------------

    print(
        "Monitoring Cellular Modem Manager..."
    )

    print(
        "Laptop ZeroTier IP:",
        LAPTOP_ZT_IP,
    )

    # --------------------------------------------------------
    # Logger state
    # --------------------------------------------------------

    start_time = time.monotonic()

    consecutive_failures = 0

    modem_monitor = ModemMonitor()

    # --------------------------------------------------------
    # Open CSV
    # --------------------------------------------------------

    with create_log_file() as file:

        print(
            "Log file (UTC filename):",
            file.name,
        )

        print(
            "Logging every",
            LOG_PERIOD_S,
            "seconds."
        )

        print(
            "Press Ctrl+C to stop.\n"
        )

        writer = csv.DictWriter(
            file,
            fieldnames=CSV_FIELDS,
        )

        writer.writeheader()

        # ====================================================
        # MAIN LOOP
        # ====================================================

        while True:

            loop_start = time.monotonic()

            error_messages = []

            # ------------------------------------------------
            # Check modem presence and read cellular metrics
            # ------------------------------------------------

            modem_sample = modem_monitor.poll()
            radio = modem_sample["radio"]

            if radio["error"]:
                error_messages.append(radio["error"])

            if (
                radio["cell_json"] is not None
                and all(
                    radio[key] is None
                    for key in (
                        "rsrp_dbm",
                        "rsrq_db",
                        "rssi_dbm",
                        "sinr_db",
                    )
                )
            ):

                error_messages.append(
                    "Cellular: modem API returned no signal readings; "
                    "see cell_json in the CSV for the raw response."
                )

            # ------------------------------------------------
            # Ping laptop over ZeroTier
            # ------------------------------------------------

            laptop = ping_stats(
                LAPTOP_ZT_IP
            )

            if laptop["reachable"]:
                consecutive_failures = 0
            else:
                consecutive_failures += 1

            # ------------------------------------------------
            # Ping public Internet
            # ------------------------------------------------

            internet = ping_stats(
                PUBLIC_PING_TARGET
            )

            # ------------------------------------------------
            # Determine routes
            # ------------------------------------------------

            laptop_interface = route_interface(
                LAPTOP_ZT_IP
            )

            internet_interface = route_interface(
                PUBLIC_PING_TARGET
            )

            # ------------------------------------------------
            # Construct log row
            # ------------------------------------------------

            row = {

                "timestamp_utc":
                    datetime.now(
                        timezone.utc
                    ).isoformat(),

                "elapsed_s":
                    round(
                        time.monotonic()
                        - start_time,
                        1,
                    ),

                # Hardware disconnects are separate from ping failures.
                "modem_id": modem_sample["modem_id"],
                "modem_present": modem_sample["modem_present"],
                "modem_event": modem_sample["modem_event"],
                "modem_disconnect_count": modem_sample["modem_disconnect_count"],
                "modem_outage_s": modem_sample["modem_outage_s"],
                "modem_checked_utc": modem_sample["modem_checked_utc"],

                # Cellular
                "rat":
                    radio["rat"],

                "cell_id":
                    radio["cell_id"],

                "pcid":
                    radio["pcid"],

                "earfcn":
                    radio["earfcn"],

                "lte_band":
                    radio["lte_band"],

                "rsrp_dbm":
                    radio["rsrp_dbm"],

                "rsrq_db":
                    radio["rsrq_db"],

                "rssi_dbm":
                    radio["rssi_dbm"],

                "sinr_db":
                    radio["sinr_db"],

                # Laptop / ZeroTier
                "laptop_reachable":
                    laptop["reachable"],

                "laptop_loss_pct":
                    laptop["loss"],

                "laptop_rtt_avg_ms":
                    laptop["avg"],

                "laptop_rtt_mdev_ms":
                    laptop["mdev"],

                "laptop_route_if":
                    laptop_interface,

                # Internet
                "internet_reachable":
                    internet["reachable"],

                "internet_loss_pct":
                    internet["loss"],

                "internet_rtt_avg_ms":
                    internet["avg"],

                "internet_rtt_mdev_ms":
                    internet["mdev"],

                "internet_egress_if":
                    internet_interface,

                # Dropout tracking
                "consecutive_laptop_failures":
                    consecutive_failures,

                # Raw modem data
                "cell_json":
                    radio["cell_json"],

                "error":
                    " | ".join(
                        error_messages
                    )[:500],
            }

            # ------------------------------------------------
            # Save
            # ------------------------------------------------

            writer.writerow(row)

            file.flush()

            # ------------------------------------------------
            # Terminal display
            # ------------------------------------------------

            modem_status = {
                True: "present",
                False: "missing",
                None: "unknown",
            }[row["modem_present"]]

            print(
                f"{row['timestamp_utc']} | "
                f"Modem={modem_status} | "
                f"Disconnects={row['modem_disconnect_count']} | "
                f"RAT={row['rat']} | "
                f"RSRP={row['rsrp_dbm']} dBm | "
                f"RSRQ={row['rsrq_db']} dB | "
                f"RSSI={row['rssi_dbm']} dBm | "
                f"SINR={row['sinr_db']} dB | "
                f"Laptop RTT="
                f"{row['laptop_rtt_avg_ms']} ms | "
                f"Loss="
                f"{row['laptop_loss_pct']}% | "
                f"Failures="
                f"{row['consecutive_laptop_failures']} | "
                f"Internet IF="
                f"{row['internet_egress_if']}"
            )

            if row["modem_event"]:
                print(
                    "  MODEM",
                    row["modem_event"].replace("_", " ").upper(),
                    f"at {row['modem_checked_utc']} | "
                    f"ID={row['modem_id']} | "
                    f"Observed outage={row['modem_outage_s']} s",
                )

            if row["error"]:

                print(
                    "  ERROR:",
                    row["error"],
                )

            # ------------------------------------------------
            # Maintain requested logging period
            # ------------------------------------------------

            loop_duration = (
                time.monotonic()
                - loop_start
            )

            sleep_time = max(
                0,
                LOG_PERIOD_S
                - loop_duration,
            )

            time.sleep(
                sleep_time
            )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print(
            "\nLogger stopped."
        )
