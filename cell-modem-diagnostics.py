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

# Time between logging cycles
LOG_PERIOD_S = 10


# CSV is saved in the same folder as this script.
LOG_FILE = Path(__file__).with_name(
    "cell_link_log.csv"
)


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

def find_modem(api_base):
    """
    Ask Cellular Modem Manager which modems are connected.

    Prefer the Quectel EG25-G used by the BlueBoat.
    """

    modems = request_json(
        f"{api_base}/modem/"
    )

    if not modems:
        raise RuntimeError(
            "Cellular Modem Manager reported no modems."
        )

    # Prefer EG25-G if there are multiple devices.
    for modem in modems:

        product = str(
            modem.get("product", "")
        ).upper()

        if "EG25" in product:

            print(
                "Using modem:",
                modem.get("manufacturer"),
                modem.get("product"),
            )

            return modem["id"]

    # Fall back to the first modem.
    modem = modems[0]

    print(
        "Using modem:",
        modem.get("manufacturer"),
        modem.get("product"),
    )

    return modem["id"]


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
    # Discover Cellular Modem Manager
    # --------------------------------------------------------

    print(
        "Finding Cellular Modem Manager..."
    )

    api_base = discover_modem_api_base()

    print(
        "Cellular Modem Manager API:",
        api_base,
    )

    # --------------------------------------------------------
    # Discover modem
    # --------------------------------------------------------

    modem_id = find_modem(
        api_base
    )

    print(
        "Modem ID:",
        modem_id,
    )

    print(
        "Laptop ZeroTier IP:",
        LAPTOP_ZT_IP,
    )

    print(
        "Log file:",
        LOG_FILE,
    )

    print(
        "Logging every",
        LOG_PERIOD_S,
        "seconds."
    )

    print(
        "Press Ctrl+C to stop.\n"
    )

    # --------------------------------------------------------
    # Logger state
    # --------------------------------------------------------

    start_time = time.monotonic()

    consecutive_failures = 0

    new_file = (
        not LOG_FILE.exists()
        or LOG_FILE.stat().st_size == 0
    )

    # --------------------------------------------------------
    # Open CSV
    # --------------------------------------------------------

    with LOG_FILE.open(
        "a",
        newline="",
        buffering=1,
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=CSV_FIELDS,
        )

        if new_file:
            writer.writeheader()

        # ====================================================
        # MAIN LOOP
        # ====================================================

        while True:

            loop_start = time.monotonic()

            error_messages = []

            # ------------------------------------------------
            # Read cellular metrics
            # ------------------------------------------------

            try:

                radio = read_cell_metrics(
                    api_base,
                    modem_id,
                )

            except Exception as first_error:

                # The extension might have restarted and
                # received a different Docker host port.
                #
                # Try rediscovery once before recording a
                # failure.
                try:

                    print(
                        "Cellular API query failed; "
                        "attempting rediscovery..."
                    )

                    api_base = (
                        discover_modem_api_base()
                    )

                    modem_id = find_modem(
                        api_base
                    )

                    radio = read_cell_metrics(
                        api_base,
                        modem_id,
                    )

                    print(
                        "Cellular API rediscovered:",
                        api_base,
                    )

                except Exception as second_error:

                    radio = empty_cell_metrics()

                    error_messages.append(
                        "Cellular: "
                        f"{type(second_error).__name__}: "
                        f"{second_error}"
                    )

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

            print(
                f"{row['timestamp_utc']} | "
                f"RAT={row['rat']} | "
                f"RSRP={row['rsrp_dbm']} dBm | "
                f"RSRQ={row['rsrq_db']} dB | "
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
