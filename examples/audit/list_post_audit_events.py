"""
List OCI Audit events from the last 24 hours where the API request HTTP method is POST.

Audit events are regional. The script scans all subscribed regions (unless -rg is set),
calls ListEvents for the tenancy OCID or a compartment filter (-c), and keeps events
whose request action is POST. Events named SummarizeMetricsData are excluded (monitoring noise).

Default time window: rolling last 24 hours (UTC). Override with -hours N.

IAM: read audit-events in the compartment(s) queried (tenancy root for full tenancy view).

Usage (from repository root):
    python examples/audit/list_post_audit_events.py
    python examples/audit/list_post_audit_events.py -rg eu-frankfurt-1
    python examples/audit/list_post_audit_events.py -hours 48
    python examples/audit/list_post_audit_events.py -c /root/prod -log post_audit.txt
    python examples/audit/list_post_audit_events.py -csv report
    python examples/audit/list_post_audit_events.py -excel report

Excel export requires: pip install openpyxl
"""

import csv
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

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
    resolve_export_paths,
    write_excel_export_file,
)

oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()

MIN_OCI_VERSION = "2.88.0"
SCRIPT_VERSION = "26.11.02"
DEFAULT_HOURS = 24
DEFAULT_CSV_EXPORT = "post_audit_events.csv"
DEFAULT_XLSX_EXPORT = "post_audit_events.xlsx"

EXPORT_COLUMNS = [
    ("region", "Region"),
    ("event_time", "Event time (UTC)"),
    ("event_name", "Event name"),
    ("compartment", "Compartment"),
    ("resource_name", "Resource name"),
    ("principal", "Principal"),
    ("request_path", "Request path"),
    ("response_status", "Status"),
    ("event_id", "Event ID"),
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
    def __init__(self, region_count):
        self.total = region_count
        self.done = 0
        self._lock = threading.Lock()

    def region_finished(self, region):
        with self._lock:
            self.done += 1
            pct = 100.0 * self.done / self.total if self.total else 100.0
            print(
                "Progress: {:5.1f}% ({}/{})  {}".format(
                    pct, self.done, self.total, region
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


def paginate_audit_events(region, audit_client, compartment_id, start_time, end_time):
    def _call(strategy):
        return oci.pagination.list_call_get_all_results(
            audit_client.list_events,
            compartment_id=compartment_id,
            start_time=start_time,
            end_time=end_time,
            retry_strategy=strategy,
        ).data

    return call_with_manual_429_retry(_call, region)


def audit_time_window(hours):
    end_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start_time = end_time - timedelta(hours=hours)
    return start_time, end_time


def format_event_time(event_time):
    if event_time is None or not isinstance(event_time, datetime):
        return "-"
    dt = event_time
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def request_action(event):
    data = event.data
    if data is None or data.request is None or data.request.action is None:
        return ""
    return str(data.request.action).strip().upper()


IGNORED_EVENT_NAMES = frozenset({"SummarizeMetricsData"})


def audit_event_name(event):
    data = event.data
    if data is not None and data.event_name:
        return data.event_name
    return event.event_type or ""


def is_post_event(event):
    return request_action(event) == "POST"


def should_include_event(event):
    if not is_post_event(event):
        return False
    return audit_event_name(event) not in IGNORED_EVENT_NAMES


def event_to_row(region, event):
    data = event.data or oci.audit.models.Data()
    identity = data.identity or oci.audit.models.Identity()
    request = data.request or oci.audit.models.Request()
    response = data.response or oci.audit.models.Response()

    principal = identity.principal_name or identity.caller_name or identity.principal_id or "-"

    return {
        "region": region,
        "event_time": format_event_time(event.event_time),
        "event_name": audit_event_name(event) or "-",
        "compartment": data.compartment_name or data.compartment_id or "-",
        "resource_name": data.resource_name or data.resource_id or "-",
        "principal": principal,
        "request_path": request.path or "-",
        "response_status": response.status or "-",
        "event_id": event.event_id or "-",
    }


def scan_region(region, config, signer, compartment_id, start_time, end_time, progress):
    regional_config = dict(config)
    regional_config["region"] = region
    audit = oci.audit.AuditClient(regional_config, signer=signer)
    rows = []
    error = None

    try:
        events = paginate_audit_events(
            region, audit, compartment_id, start_time, end_time
        )
        for event in events:
            if should_include_event(event):
                rows.append(event_to_row(region, event))
    except oci.exceptions.ServiceError as e:
        if e.status not in (403, 404):
            error = e.message

    progress.region_finished(region)
    return region, rows, error


def _sorted_rows(all_rows):
    return sorted(
        all_rows,
        key=lambda r: (r["event_time"], r["region"], r["event_name"]),
    )


def _export_records(rows):
    return [{key: row.get(key, "") for key, _ in EXPORT_COLUMNS} for row in rows]


def write_csv_export(path, rows):
    records = _export_records(rows)
    fieldnames = [key for key, _ in EXPORT_COLUMNS]
    headers = {key: title for key, title in EXPORT_COLUMNS}
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writerow(headers)
        writer.writerows(records)


def write_excel_export(path, rows):
    write_excel_export_file(
        path,
        EXPORT_COLUMNS,
        _export_records(rows),
        sheet_title="POST audit events",
    )


def parse_cmd():
    hours = DEFAULT_HOURS
    argv = sys.argv[1:]
    filtered = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-hours", "--hours"):
            if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                print("ERROR: -hours requires a positive integer (e.g. -hours 24)")
                sys.exit(1)
            try:
                hours = int(argv[i + 1])
                if hours <= 0:
                    raise ValueError
            except ValueError:
                print("ERROR: -hours must be a positive integer")
                sys.exit(1)
            i += 2
            continue
        filtered.append(arg)
        i += 1
    sys.argv = [sys.argv[0]] + filtered
    cmd = input_command_line(export_options=True)
    resolve_export_paths(cmd, DEFAULT_CSV_EXPORT, DEFAULT_XLSX_EXPORT)
    cmd.audit_hours = hours
    return cmd


def main():
    check_oci_version(MIN_OCI_VERSION)
    cmd = parse_cmd()

    writer = None
    if cmd.log_file:
        writer = MyWriter(sys.stdout, cmd.log_file)
        sys.stdout = writer

    try:
        print("Script: list_post_audit_events.py  Version: {}".format(SCRIPT_VERSION))
        print("Listing audit events with HTTP POST in the last {} hours...".format(cmd.audit_hours))
        print("")

        config, signer = create_signer(
            cmd.config_profile,
            cmd.is_instance_principals,
            cmd.is_delegation_token,
            cmd.is_security_token,
            interactive=(cmd.interactive_profile != ""),
            region=cmd.region,
        )
        tenancy_id = config["tenancy"]

        login(config, signer)
        tenant_name = GetTenantName(config, signer)
        print("Tenant: {} ({})".format(tenant_name, tenancy_id))

        start_time, end_time = audit_time_window(cmd.audit_hours)
        print("Audit window (UTC): {}  to  {}".format(
            start_time.strftime("%Y-%m-%d %H:%M:%S"),
            end_time.strftime("%Y-%m-%d %H:%M:%S"),
        ))

        subscribed = SubscribedRegions(config, signer)
        if cmd.region:
            if cmd.region not in subscribed:
                print("Region '{}' is not subscribed in this tenancy, aborting".format(cmd.region))
                sys.exit(1)
            regions = [cmd.region]
        else:
            regions = sorted(subscribed)

        audit_compartment_id = tenancy_id
        if cmd.compartment:
            compartments = GetFullCompartments(config, signer)
            selected = FindCompartment(compartments, cmd.compartment)
            if selected is None:
                print("Compartment '{}' not found, aborting".format(cmd.compartment))
                sys.exit(1)
            audit_compartment_id = selected.details.id
            print("Compartment filter: {} ({})".format(selected.fullpath, audit_compartment_id))

        print("Regions to scan ({}): {}".format(len(regions), ", ".join(regions)))
        print("")

        progress = ScanProgress(len(regions))
        all_rows = []
        region_errors = []

        with ThreadPoolExecutor(max_workers=region_worker_count(regions)) as executor:
            futures = {
                executor.submit(
                    scan_region,
                    region,
                    config,
                    signer,
                    audit_compartment_id,
                    start_time,
                    end_time,
                    progress,
                ): region
                for region in regions
            }
            for future in as_completed(futures):
                region, rows, error = future.result()
                all_rows.extend(rows)
                if error:
                    region_errors.append((region, error))

        print("")
        print("")

        sorted_rows = _sorted_rows(all_rows)
        header = "{:<16} {:<22} {:<28} {:<24} {:<24} {:>6}".format(
            "Region",
            "Event time (UTC)",
            "Event name",
            "Compartment",
            "Principal",
            "Status",
        )
        print(header)
        print("-" * len(header))

        for row in sorted_rows:
            print(
                "{:<16} {:<22} {:<28} {:<24} {:<24} {:>6}".format(
                    row["region"],
                    row["event_time"],
                    _truncate(row["event_name"], 28),
                    _truncate(row["compartment"], 24),
                    _truncate(row["principal"], 24),
                    _truncate(str(row["response_status"]), 6),
                )
            )

        print("")
        print("Total POST audit events: {}".format(len(sorted_rows)))

        if cmd.csv_export:
            write_csv_export(cmd.csv_export, sorted_rows)
            print("CSV export written: {}".format(os.path.abspath(cmd.csv_export)))
        if cmd.excel_export:
            write_excel_export(cmd.excel_export, sorted_rows)
            print("Excel export written: {}".format(os.path.abspath(cmd.excel_export)))

        if region_errors:
            print("Non-fatal errors (first 10):")
            for region, message in region_errors[:10]:
                print("  {}  {}".format(region, message))
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


if __name__ == "__main__":
    main()
