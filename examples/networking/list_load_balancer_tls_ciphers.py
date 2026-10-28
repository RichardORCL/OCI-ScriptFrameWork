"""
List OCI Load Balancer (LBaaS) load balancers and TLS cipher configuration per listener.

Uses the Load Balancing service API (oci.load_balancer). Network Load Balancers are
not included; they do not expose the same SSL cipher suite model.

For each HTTPS/SSL listener, reports the cipher suite name, enabled TLS protocol versions,
and the cipher list (custom suites from the load balancer, or Oracle predefined suites
resolved from the installed OCI SDK documentation).

One row per listener. Plain HTTP listeners are listed with TLS fields set to "-".

One thread per region; progress updates per completed compartment
(compartments x regions = 100%).

Usage (from repository root):
    python examples/networking/list_load_balancer_tls_ciphers.py
    python examples/networking/list_load_balancer_tls_ciphers.py -rg eu-frankfurt-1
    python examples/networking/list_load_balancer_tls_ciphers.py -c /root/prod -log lb_tls.txt
    python examples/networking/list_load_balancer_tls_ciphers.py -csv report
    python examples/networking/list_load_balancer_tls_ciphers.py -excel report

Excel export requires: pip install openpyxl
"""

import csv
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import oci
from oci.retry import RetryStrategyBuilder

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from ocimodules.functions import (
    login,
    input_command_line,
    create_signer,
    check_oci_version,
    MyWriter,
    GetTenantName,
    SubscribedRegions,
    GetFullCompartments,
    FindCompartment,
    get_owner,
)

oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()

MIN_OCI_VERSION = "2.88.0"
SCRIPT_VERSION = "26.10.28"
DEFAULT_CSV_EXPORT = "load_balancer_tls_ciphers.csv"
DEFAULT_XLSX_EXPORT = "load_balancer_tls_ciphers.xlsx"
DEFAULT_PREDEFINED_CIPHER_SUITE = "oci-default-ssl-cipher-suite-v1"
TLS_LISTENER_PROTOCOLS = frozenset({"HTTPS", "SSL"})

EXPORT_COLUMNS = [
    ("region", "Region"),
    ("compartment_path", "Compartment"),
    ("lb_name", "Load balancer"),
    ("lifecycle_state", "State"),
    ("listener", "Listener"),
    ("port", "Port"),
    ("protocol", "Protocol"),
    ("tls_protocols", "TLS protocols"),
    ("cipher_suite", "Cipher suite"),
    ("ciphers", "Ciphers"),
    ("owner", "Owner"),
    ("load_balancer_id", "Load balancer OCID"),
]

_region_throttle_active = {}
_region_throttle_lock = threading.Lock()

THROTTLE_RETRY_STRATEGY = RetryStrategyBuilder(
    max_attempts_check=True,
    max_attempts=15,
    total_elapsed_time_check=False,
    retry_max_wait_between_calls=120,
    retry_base_sleep_time=5,
    backoff_type=oci.retry.BACKOFF_FULL_JITTER_EQUAL_ON_THROTTLE_VALUE,
).add_service_error_check(
    service_error_retry_config=oci.retry.retry_checkers.RETRYABLE_STATUSES_AND_CODES,
).get_retry_strategy()

MANUAL_429_MAX_ATTEMPTS = 12
MANUAL_429_BASE_SLEEP = 8


class ScanProgress:
    def __init__(self, compartment_count, region_count):
        self.total = compartment_count * region_count
        self.done = 0
        self._lock = threading.Lock()

    def compartment_finished(self, region, compartment_path):
        with self._lock:
            self.done += 1
            pct = 100.0 * self.done / self.total if self.total else 100.0
            print(
                "Progress: {:5.1f}% ({}/{})  {}  {}".format(
                    pct, self.done, self.total, region, _truncate(compartment_path, 36)
                ),
                end="\r",
                flush=True,
            )


def mark_region_throttled(region):
    with _region_throttle_lock:
        _region_throttle_active[region] = True


def retry_strategy_for_region(region):
    with _region_throttle_lock:
        if _region_throttle_active.get(region):
            return THROTTLE_RETRY_STRATEGY
    return oci.retry.DEFAULT_RETRY_STRATEGY


def region_worker_count(regions):
    return max(1, len(regions))


def call_with_manual_429_retry(operation, region):
    last_error = None
    for attempt in range(MANUAL_429_MAX_ATTEMPTS):
        try:
            strategy = retry_strategy_for_region(region)
            return operation(strategy)
        except oci.exceptions.ServiceError as e:
            last_error = e
            if e.status == 429:
                mark_region_throttled(region)
                if attempt < MANUAL_429_MAX_ATTEMPTS - 1:
                    time.sleep(min(120, MANUAL_429_BASE_SLEEP * (2 ** attempt)))
                    continue
            raise
    if last_error:
        raise last_error


def paginate(region, list_func, **kwargs):
    def _call(strategy):
        return oci.pagination.list_call_get_all_results(
            list_func, retry_strategy=strategy, **kwargs
        ).data

    return call_with_manual_429_retry(_call, region)


@lru_cache(maxsize=1)
def predefined_cipher_suites():
    """Oracle reserved suite names -> cipher list (from OCI SDK model documentation)."""
    doc = oci.load_balancer.models.SSLCipherSuite.__doc__ or ""
    parts = re.split(r"\*\s+__([a-z0-9-]+)__", doc)
    suites = {}
    for i in range(1, len(parts), 2):
        name = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        chunk = body.split("*  __", 1)[0]
        ciphers = re.findall(r'"([^"]+)"', chunk)
        if ciphers:
            suites[name] = tuple(ciphers)
    return suites


def format_protocols(protocols):
    if not protocols:
        return "-"
    return ", ".join(protocols)


def format_cipher_list(ciphers):
    if not ciphers:
        return "-"
    if isinstance(ciphers, (list, tuple)):
        return "; ".join(ciphers)
    return str(ciphers)


def resolve_cipher_list(load_balancer, cipher_suite_name):
    if not cipher_suite_name:
        return []

    custom = getattr(load_balancer, "ssl_cipher_suites", None) or {}
    if cipher_suite_name in custom and custom[cipher_suite_name].ciphers:
        return list(custom[cipher_suite_name].ciphers)

    predefined = predefined_cipher_suites()
    if cipher_suite_name in predefined:
        return list(predefined[cipher_suite_name])

    return []


def listener_tls_details(listener):
    protocol = (listener.protocol or "").upper()
    if protocol not in TLS_LISTENER_PROTOCOLS:
        return {
            "tls_protocols": "-",
            "cipher_suite": "-",
            "ciphers": "-",
        }

    ssl_cfg = listener.ssl_configuration
    if ssl_cfg is None:
        suite_name = DEFAULT_PREDEFINED_CIPHER_SUITE
        tls_protocols = "-"
    else:
        suite_name = ssl_cfg.cipher_suite_name or DEFAULT_PREDEFINED_CIPHER_SUITE
        tls_protocols = format_protocols(ssl_cfg.protocols)

    return {
        "tls_protocols": tls_protocols,
        "cipher_suite": suite_name,
        "ciphers": None,
    }


def rows_for_load_balancer(region, compartment_path, load_balancer, config, config_profile):
    owner_info = get_owner(
        load_balancer.defined_tags, config=config, config_profile=config_profile
    )
    owner = owner_info.get("owner") or "-"
    lb_name = load_balancer.display_name or load_balancer.id
    lifecycle = load_balancer.lifecycle_state or "-"
    lb_id = load_balancer.id

    listeners = load_balancer.listeners or {}
    if not listeners:
        return [
            {
                "region": region,
                "compartment_path": compartment_path,
                "lb_name": lb_name,
                "lifecycle_state": lifecycle,
                "listener": "-",
                "port": "-",
                "protocol": "-",
                "tls_protocols": "-",
                "cipher_suite": "-",
                "ciphers": "-",
                "owner": owner,
                "load_balancer_id": lb_id,
            }
        ]

    rows = []
    for listener_name in sorted(listeners.keys()):
        listener = listeners[listener_name]
        tls = listener_tls_details(listener)
        cipher_suite = tls["cipher_suite"]
        if tls["ciphers"] is None and cipher_suite != "-":
            ciphers = format_cipher_list(
                resolve_cipher_list(load_balancer, cipher_suite)
            )
            if ciphers == "-":
                ciphers = "(suite defined; cipher list not available)"
        else:
            ciphers = tls["ciphers"]

        rows.append(
            {
                "region": region,
                "compartment_path": compartment_path,
                "lb_name": lb_name,
                "lifecycle_state": lifecycle,
                "listener": listener_name,
                "port": listener.port if listener.port is not None else "-",
                "protocol": listener.protocol or "-",
                "tls_protocols": tls["tls_protocols"],
                "cipher_suite": cipher_suite,
                "ciphers": ciphers,
                "owner": owner,
                "load_balancer_id": lb_id,
            }
        )
    return rows


def get_load_balancer_details(client, load_balancer_id, region):
    def _call(strategy):
        return client.get_load_balancer(
            load_balancer_id, retry_strategy=strategy
        ).data

    return call_with_manual_429_retry(_call, region)


def scan_compartment(region, lb_client, compartment_id, compartment_path, config, config_profile):
    rows = []
    errors = []

    try:
        summaries = paginate(
            region, lb_client.list_load_balancers, compartment_id=compartment_id
        )
    except oci.exceptions.ServiceError as e:
        if e.status in (403, 404):
            return rows, errors
        return rows, [(compartment_path, "list load balancers: {}".format(e.message))]

    for summary in summaries:
        if summary.lifecycle_state in ("DELETED", "DELETING"):
            continue
        try:
            lb = get_load_balancer_details(lb_client, summary.id, region)
        except oci.exceptions.ServiceError as e:
            if e.status not in (403, 404):
                label = summary.display_name or summary.id
                errors.append((compartment_path, "{}: {}".format(label, e.message)))
            continue

        rows.extend(
            rows_for_load_balancer(region, compartment_path, lb, config, config_profile)
        )

    return rows, errors


def scan_region(
    config, signer, region, compartments_to_check, config_profile, progress
):
    regional_config = dict(config)
    regional_config["region"] = region
    lb_client = oci.load_balancer.LoadBalancerClient(regional_config, signer=signer)

    rows = []
    errors = []
    for comp in compartments_to_check:
        comp_rows, comp_errors = scan_compartment(
            region,
            lb_client,
            comp.details.id,
            comp.fullpath,
            config,
            config_profile,
        )
        rows.extend(comp_rows)
        for path, message in comp_errors:
            errors.append((path, message))
        progress.compartment_finished(region, comp.fullpath)

    return region, rows, errors


def compartments_to_scan(all_compartments, selected_compartment_id):
    if not selected_compartment_id:
        return all_compartments
    return [
        c
        for c in all_compartments
        if _instance_in_compartment_subtree(
            c.details.id, selected_compartment_id, all_compartments
        )
    ]


def _sorted_rows(all_rows):
    return sorted(
        all_rows,
        key=lambda r: (
            r["region"],
            r["compartment_path"],
            r["lb_name"],
            str(r["listener"]),
        ),
    )


def _export_records(rows):
    return [{key: row.get(key, "") for key, _ in EXPORT_COLUMNS} for row in rows]


def _ensure_export_extension(path, extension):
    if not path.lower().endswith(extension.lower()):
        return path + extension
    return path


def write_csv_export(path, rows):
    records = _export_records(rows)
    fieldnames = [key for key, _ in EXPORT_COLUMNS]
    headers = {key: title for key, title in EXPORT_COLUMNS}
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writerow(headers)
        writer.writerows(records)


def write_excel_export(path, rows):
    try:
        from openpyxl import Workbook
    except ImportError:
        print("ERROR: Excel export requires openpyxl. Install with: pip install openpyxl")
        sys.exit(1)

    records = _export_records(rows)
    wb = Workbook()
    ws = wb.active
    ws.title = "LB TLS ciphers"
    ws.append([title for _, title in EXPORT_COLUMNS])
    for record in records:
        ws.append([record[key] for key, _ in EXPORT_COLUMNS])
    wb.save(path)


def parse_cmd():
    csv_path = ""
    xlsx_path = ""
    argv = sys.argv[1:]
    filtered = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-csv", "--csv"):
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                csv_path = _ensure_export_extension(argv[i + 1], ".csv")
                i += 2
            else:
                csv_path = DEFAULT_CSV_EXPORT
                i += 1
            continue
        if arg in ("-excel", "--excel", "-xlsx", "--xlsx"):
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                xlsx_path = _ensure_export_extension(argv[i + 1], ".xlsx")
                i += 2
            else:
                xlsx_path = DEFAULT_XLSX_EXPORT
                i += 1
            continue
        filtered.append(arg)
        i += 1
    sys.argv = [sys.argv[0]] + filtered
    cmd = input_command_line()
    cmd.csv_export = csv_path
    cmd.excel_export = xlsx_path
    return cmd


def main():
    check_oci_version(MIN_OCI_VERSION)
    cmd = parse_cmd()

    writer = None
    if cmd.log_file:
        writer = MyWriter(sys.stdout, cmd.log_file)
        sys.stdout = writer

    try:
        print("Script: list_load_balancer_tls_ciphers.py  Version: {}".format(SCRIPT_VERSION))
        print("Listing OCI Load Balancer listeners and TLS cipher configuration...")
        print("")

        config, signer = create_signer(
            cmd.config_profile,
            cmd.is_instance_principals,
            cmd.is_delegation_token,
            cmd.is_security_token,
            interactive=(cmd.interactive_profile != ""),
            region=cmd.region,
        )

        login(config, signer)
        tenant_name = GetTenantName(config, signer)
        print("Tenant: {} ({})".format(tenant_name, config["tenancy"]))

        subscribed = SubscribedRegions(config, signer)
        if cmd.region:
            if cmd.region not in subscribed:
                print("Region '{}' is not subscribed in this tenancy, aborting".format(cmd.region))
                sys.exit(1)
            regions = [cmd.region]
        else:
            regions = sorted(subscribed)

        print("Regions to scan ({}): {}".format(len(regions), ", ".join(regions)))
        print("")

        compartments = GetFullCompartments(config, signer)
        selected_compartment_id = None
        if cmd.compartment:
            selected = FindCompartment(compartments, cmd.compartment)
            if selected is None:
                print("Compartment '{}' not found, aborting".format(cmd.compartment))
                sys.exit(1)
            selected_compartment_id = selected.details.id
            print("Compartment filter: {} ({})".format(selected.fullpath, selected_compartment_id))
            print("")

        scan_compartments = compartments_to_scan(compartments, selected_compartment_id)
        progress = ScanProgress(len(scan_compartments), len(regions))
        workers = region_worker_count(regions)

        all_rows = []
        region_errors = []

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    scan_region,
                    config,
                    signer,
                    region,
                    scan_compartments,
                    cmd.config_profile,
                    progress,
                ): region
                for region in regions
            }
            for future in as_completed(futures):
                region, rows, errors = future.result()
                all_rows.extend(rows)
                for path, message in errors:
                    region_errors.append((region, path, message))

        print("")
        print("")

        sorted_rows = _sorted_rows(all_rows)
        header = "{:<18} {:<24} {:<22} {:<14} {:>5} {:<8} {:<28} {:<22}".format(
            "Region",
            "Compartment",
            "Load balancer",
            "Listener",
            "Port",
            "Protocol",
            "Cipher suite",
            "Ciphers (preview)",
        )
        print(header)
        print("-" * len(header))

        for row in sorted_rows:
            print(
                "{:<18} {:<24} {:<22} {:<14} {:>5} {:<8} {:<28} {:<22}".format(
                    row["region"],
                    _truncate(row["compartment_path"], 24),
                    _truncate(row["lb_name"], 22),
                    _truncate(row["listener"], 14),
                    row["port"],
                    _truncate(str(row["protocol"]), 8),
                    _truncate(str(row["cipher_suite"]), 28),
                    _truncate(str(row["ciphers"]), 22),
                )
            )

        print("")
        lb_ids = {r["load_balancer_id"] for r in sorted_rows}
        tls_rows = [
            r
            for r in sorted_rows
            if str(r.get("protocol", "")).upper() in TLS_LISTENER_PROTOCOLS
        ]
        print("Load balancers (listener rows): {}  ({} unique load balancers)".format(
            len(sorted_rows), len(lb_ids)
        ))
        print("  HTTPS/SSL listener rows: {}".format(len(tls_rows)))

        if cmd.csv_export:
            write_csv_export(cmd.csv_export, sorted_rows)
            print("CSV export written: {}".format(os.path.abspath(cmd.csv_export)))
        if cmd.excel_export:
            write_excel_export(cmd.excel_export, sorted_rows)
            print("Excel export written: {}".format(os.path.abspath(cmd.excel_export)))

        if region_errors:
            print("Non-fatal errors (first 10):")
            for region, path, message in region_errors[:10]:
                print("  {}  {}  {}".format(region, path, message))
            if len(region_errors) > 10:
                print("  ... and {} more".format(len(region_errors) - 10))

    finally:
        if writer is not None:
            sys.stdout = writer.stdout
            writer.close()


def _truncate(text, max_len):
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _instance_in_compartment_subtree(instance_compartment_id, selected_compartment_id, compartments):
    if instance_compartment_id == selected_compartment_id:
        return True
    path_by_id = {c.details.id: c.fullpath for c in compartments}
    selected_path = path_by_id.get(selected_compartment_id)
    instance_path = path_by_id.get(instance_compartment_id)
    if not selected_path or not instance_path:
        return instance_compartment_id == selected_compartment_id
    return instance_path == selected_path or instance_path.startswith(selected_path + "/")


if __name__ == "__main__":
    main()
