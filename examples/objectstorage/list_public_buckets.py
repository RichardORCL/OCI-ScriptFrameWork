"""
List Object Storage buckets that allow public access (not NoPublicAccess).

For each bucket in each compartment and region, calls GetBucket and keeps rows where
publicAccessType is ObjectRead or ObjectReadWithoutList.

Output columns: region, compartment path, bucket name, public access type, namespace,
owner (defined tags via get_owner), age in days (from time_created).

One thread per region; progress updates per completed compartment
(compartments x regions = 100%).

Usage (from repository root):
    python examples/objectstorage/list_public_buckets.py
    python examples/objectstorage/list_public_buckets.py -rg eu-frankfurt-1
    python examples/objectstorage/list_public_buckets.py -c /root/prod -log public_buckets.txt
    python examples/objectstorage/list_public_buckets.py -csv report
    python examples/objectstorage/list_public_buckets.py -excel report

Excel export requires: pip install openpyxl
"""

import csv
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

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
    GetHomeRegion,
    SubscribedRegions,
    GetFullCompartments,
    FindCompartment,
    get_owner,
    resolve_export_paths,
    write_excel_export_file,
)

EXCEL_INT_COLUMNS = frozenset({"age"})

oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()

MIN_OCI_VERSION = "2.88.0"
SCRIPT_VERSION = "26.10.28"
DEFAULT_CSV_EXPORT = "public_buckets.csv"
DEFAULT_XLSX_EXPORT = "public_buckets.xlsx"

NO_PUBLIC = oci.object_storage.models.Bucket.PUBLIC_ACCESS_TYPE_NO_PUBLIC_ACCESS
OBJECT_READ = oci.object_storage.models.Bucket.PUBLIC_ACCESS_TYPE_OBJECT_READ
OBJECT_READ_NO_LIST = (
    oci.object_storage.models.Bucket.PUBLIC_ACCESS_TYPE_OBJECT_READ_WITHOUT_LIST
)

EXPORT_COLUMNS = [
    ("region", "Region"),
    ("compartment_path", "Compartment"),
    ("name", "Bucket name"),
    ("public_access", "Public access"),
    ("namespace", "Namespace"),
    ("age", "Days"),
    ("owner", "Owner"),
    ("bucket_id", "Bucket OCID"),
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

    def skip_remaining_compartments(self, region, count, reason=""):
        if count <= 0:
            return
        with self._lock:
            self.done += count
            label = reason if reason else "skipped"
            pct = 100.0 * self.done / self.total if self.total else 100.0
            print(
                "Progress: {:5.1f}% ({}/{})  {}  {}".format(
                    pct, self.done, self.total, region, _truncate(str(label), 36)
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


def is_public_access_type(public_access_type):
    if not public_access_type:
        return False
    if public_access_type == NO_PUBLIC:
        return False
    return True


def public_access_label(public_access_type):
    if public_access_type == OBJECT_READ:
        return "ObjectRead"
    if public_access_type == OBJECT_READ_NO_LIST:
        return "ObjectReadWithoutList"
    return public_access_type or "Unknown"


def format_age_days(time_created):
    if time_created is None or not isinstance(time_created, datetime):
        return "-"
    dt = time_created
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days
    if days < 0:
        return "0"
    return str(days)


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


def get_bucket_details(object_storage, namespace, bucket_name, region):
    def _call(strategy):
        return object_storage.get_bucket(
            namespace,
            bucket_name,
            retry_strategy=strategy,
        ).data

    return call_with_manual_429_retry(_call, region)


def scan_compartment(
    region,
    object_storage,
    namespace,
    compartment_id,
    compartment_path,
    config,
    config_profile,
):
    rows = []
    errors = []

    try:
        summaries = paginate(
            region,
            object_storage.list_buckets,
            namespace_name=namespace,
            compartment_id=compartment_id,
        )
    except oci.exceptions.ServiceError as e:
        if e.status in (403, 404):
            return rows, errors
        return rows, [(compartment_path, "list buckets: {}".format(e.message))]

    for summary in summaries:
        name = summary.name
        if not name:
            continue
        try:
            bucket = get_bucket_details(object_storage, namespace, name, region)
        except oci.exceptions.ServiceError as e:
            if e.status not in (403, 404):
                errors.append((compartment_path, "{}: {}".format(name, e.message)))
            continue

        if not is_public_access_type(bucket.public_access_type):
            continue

        owner_info = get_owner(
            bucket.defined_tags, config=config, config_profile=config_profile
        )
        rows.append(
            {
                "region": region,
                "compartment_path": compartment_path,
                "name": name,
                "public_access": public_access_label(bucket.public_access_type),
                "namespace": bucket.namespace or namespace,
                "age": format_age_days(getattr(bucket, "time_created", None)),
                "owner": owner_info.get("owner") or "-",
                "bucket_id": bucket.id or "-",
            }
        )

    return rows, errors


def scan_region(
    config,
    signer,
    namespace,
    region,
    compartments_to_check,
    config_profile,
    progress,
):
    regional_config = dict(config)
    regional_config["region"] = region
    object_storage = oci.object_storage.ObjectStorageClient(regional_config, signer=signer)

    rows = []
    errors = []
    for comp in compartments_to_check:
        comp_rows, comp_errors = scan_compartment(
            region,
            object_storage,
            namespace,
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


def fetch_object_storage_namespace(config, signer):
    home = GetHomeRegion(config, signer) or config.get("region")
    if not home:
        raise RuntimeError("Could not determine home region for get_namespace")
    regional_config = dict(config)
    regional_config["region"] = home
    client = oci.object_storage.ObjectStorageClient(regional_config, signer=signer)
    return client.get_namespace().data


def _sorted_rows(all_rows):
    return sorted(
        all_rows,
        key=lambda r: (r["region"], r["compartment_path"], r["name"]),
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
        sheet_title="Public buckets",
        int_columns=EXCEL_INT_COLUMNS,
    )


def parse_cmd():
    cmd = input_command_line(export_options=True)
    resolve_export_paths(cmd, DEFAULT_CSV_EXPORT, DEFAULT_XLSX_EXPORT)
    return cmd


def main():
    check_oci_version(MIN_OCI_VERSION)
    cmd = parse_cmd()

    writer = None
    if cmd.log_file:
        writer = MyWriter(sys.stdout, cmd.log_file)
        sys.stdout = writer

    try:
        print("Script: list_public_buckets.py  Version: {}".format(SCRIPT_VERSION))
        print("Listing Object Storage buckets with public access enabled...")
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

        try:
            namespace = fetch_object_storage_namespace(config, signer)
        except oci.exceptions.ServiceError as e:
            print("ERROR: get_namespace failed: {}".format(e.message))
            sys.exit(1)
        print("Object Storage namespace: {}".format(namespace))
        print("")

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
        workers = region_worker_count(regions)
        progress = ScanProgress(len(scan_compartments), len(regions))

        all_rows = []
        region_errors = []

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    scan_region,
                    config,
                    signer,
                    namespace,
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

        header = "{:<18} {:<28} {:<26} {:<24} {:>8}  {:<24}".format(
            "Region", "Compartment", "Bucket name", "Public access", "Days", "Owner"
        )
        print(header)
        print("-" * len(header))

        sorted_rows = _sorted_rows(all_rows)
        for row in sorted_rows:
            print(
                "{:<18} {:<28} {:<26} {:<24} {:>8}  {:<24}".format(
                    row["region"],
                    _truncate(row["compartment_path"], 28),
                    _truncate(row["name"], 26),
                    _truncate(row["public_access"], 24),
                    row["age"],
                    _truncate(row["owner"], 24),
                )
            )

        print("")
        read_count = sum(1 for r in all_rows if r["public_access"] == "ObjectRead")
        read_no_list = sum(
            1 for r in all_rows if r["public_access"] == "ObjectReadWithoutList"
        )
        other_count = len(all_rows) - read_count - read_no_list

        print("Total buckets with public access: {}".format(len(all_rows)))
        print("  ObjectRead: {}".format(read_count))
        print("  ObjectReadWithoutList: {}".format(read_no_list))
        if other_count:
            print("  other/unknown: {}".format(other_count))

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
