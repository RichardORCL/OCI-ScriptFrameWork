"""
List boot and block volumes that are not attached to a compute instance.

Scans subscribed regions in parallel (one thread per region). Within each region,
compartments are processed sequentially. A volume is unattached when no compute
VolumeAttachment / BootVolumeAttachment references it with lifecycle ATTACHED.

Output columns: region, volume type, compartment path, display name, size (GB),
VPU per GB, owner (from defined tags via get_owner / ~/.oci/config), age in days
(from the volume object's time_created).

Usage (from repository root):
    python examples/blockvolume/list_unattached_volumes.py
    python examples/blockvolume/list_unattached_volumes.py -rg eu-frankfurt-1
    python examples/blockvolume/list_unattached_volumes.py -c /root/prod -log unattached.txt
    python examples/blockvolume/list_unattached_volumes.py -csv report
    python examples/blockvolume/list_unattached_volumes.py -excel report
    python examples/blockvolume/list_unattached_volumes.py -minage 90

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
    SubscribedRegions,
    GetFullCompartments,
    FindCompartment,
    get_owner,
)

oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()

MIN_OCI_VERSION = "2.88.0"
SCRIPT_VERSION = "26.10.28"
DEFAULT_CSV_EXPORT = "unattached_volumes.csv"
DEFAULT_XLSX_EXPORT = "unattached_volumes.xlsx"

EXPORT_COLUMNS = [
    ("region", "Region"),
    ("type", "Type"),
    ("compartment_path", "Compartment"),
    ("name", "Volume name"),
    ("size_gb", "SizeGB"),
    ("vpu", "VPU"),
    ("age", "Days"),
    ("owner", "Owner"),
    ("volume_id", "Volume OCID"),
    ("lifecycle_state", "State"),
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

ATTACHED = oci.core.models.VolumeAttachment.LIFECYCLE_STATE_ATTACHED


class ScanProgress:
    """Progress across all (region, compartment) pairs: compartments * regions = 100%."""

    def __init__(self, compartment_count, region_count):
        self.total = compartment_count * region_count
        self.done = 0
        self._lock = threading.Lock()

    def compartment_finished(self, region, compartment_path):
        with self._lock:
            self.done += 1
            self._print_progress(region, compartment_path)

    def skip_remaining_compartments(self, region, count, reason=""):
        """When a whole region cannot be scanned, advance progress for its compartments."""
        if count <= 0:
            return
        with self._lock:
            self.done += count
            label = reason if reason else "skipped"
            self._print_progress(region, label)

    def _print_progress(self, region, detail):
        if self.total <= 0:
            pct = 100.0
        else:
            pct = 100.0 * self.done / self.total
        print(
            "Progress: {:5.1f}% ({}/{})  {}  {}".format(
                pct, self.done, self.total, region, _truncate(str(detail), 36)
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


def format_volume_age_days(time_created):
    """Age in whole days from OCI volume.time_created (boot or block volume object)."""
    if time_created is None or not isinstance(time_created, datetime):
        return "-"
    dt = time_created
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days
    if days < 0:
        return "0"
    return str(days)


def vpu_display(volume):
    vpu = volume.vpus_per_gb
    if vpu is None and getattr(volume, "auto_tuned_vpus_per_gb", None) is not None:
        return str(volume.auto_tuned_vpus_per_gb) + " (auto)"
    if vpu is None:
        return "-"
    return str(vpu)


def compartments_to_scan(all_compartments, selected_compartment_id):
    if not selected_compartment_id:
        return all_compartments
    return [
        c for c in all_compartments
        if _instance_in_compartment_subtree(c.details.id, selected_compartment_id, all_compartments)
    ]


def get_availability_domains(identity, tenancy_id, region):
    return paginate(
        region,
        identity.list_availability_domains,
        compartment_id=tenancy_id,
    )


def attached_volume_ids(compute, compartment_id, region, availability_domains):
    block_ids = set()
    boot_ids = set()

    for attachment in paginate(region, compute.list_volume_attachments, compartment_id=compartment_id):
        if attachment.lifecycle_state == ATTACHED and attachment.volume_id:
            block_ids.add(attachment.volume_id)

    for ad in availability_domains:
        for attachment in paginate(
            region,
            compute.list_boot_volume_attachments,
            availability_domain=ad.name,
            compartment_id=compartment_id,
        ):
            if attachment.lifecycle_state == ATTACHED and attachment.boot_volume_id:
                boot_ids.add(attachment.boot_volume_id)

    return block_ids, boot_ids


def scan_compartment(region, blockstorage, compute, availability_domains, compartment_id, compartment_path, config, config_profile):
    rows = []
    errors = []

    try:
        block_attached, boot_attached = attached_volume_ids(
            compute, compartment_id, region, availability_domains
        )
    except oci.exceptions.ServiceError as e:
        if e.status in (403, 404):
            return rows, errors
        return rows, [(compartment_path, "attachments: {}".format(e.message))]

    try:
        for volume in paginate(region, blockstorage.list_volumes, compartment_id=compartment_id):
            if volume.lifecycle_state in ("TERMINATED", "TERMINATING"):
                continue
            if volume.id in block_attached:
                continue
            rows.append(_volume_row(region, "block", compartment_path, volume, config, config_profile))
    except oci.exceptions.ServiceError as e:
        if e.status not in (403, 404):
            errors.append((compartment_path, "block volumes: {}".format(e.message)))

    try:
        for volume in paginate(region, blockstorage.list_boot_volumes, compartment_id=compartment_id):
            if volume.lifecycle_state in ("TERMINATED", "TERMINATING"):
                continue
            if volume.id in boot_attached:
                continue
            rows.append(_volume_row(region, "boot", compartment_path, volume, config, config_profile))
    except oci.exceptions.ServiceError as e:
        if e.status not in (403, 404):
            errors.append((compartment_path, "boot volumes: {}".format(e.message)))

    return rows, errors


def _volume_row(region, volume_type, compartment_path, volume, config, config_profile):
    owner_info = get_owner(volume.defined_tags, config=config, config_profile=config_profile)
    return {
        "region": region,
        "type": volume_type,
        "compartment_path": compartment_path,
        "name": volume.display_name or "",
        "size_gb": volume.size_in_gbs if volume.size_in_gbs is not None else "-",
        "vpu": vpu_display(volume),
        "owner": owner_info.get("owner") or "-",
        "age": format_volume_age_days(getattr(volume, "time_created", None)),
        "volume_id": volume.id,
        "lifecycle_state": volume.lifecycle_state,
    }


def scan_region(config, signer, tenancy_id, region, compartments_to_check, config_profile, progress):
    regional_config = dict(config)
    regional_config["region"] = region
    blockstorage = oci.core.BlockstorageClient(regional_config, signer=signer)
    compute = oci.core.ComputeClient(regional_config, signer=signer)
    identity = oci.identity.IdentityClient(regional_config, signer=signer)
    try:
        availability_domains = get_availability_domains(identity, tenancy_id, region)
    except oci.exceptions.ServiceError as e:
        progress.skip_remaining_compartments(region, len(compartments_to_check), "AD list failed")
        return region, [], [("/region", "availability domains: {}".format(e.message))]

    rows = []
    errors = []
    for comp in compartments_to_check:
        comp_rows, comp_errors = scan_compartment(
            region,
            blockstorage,
            compute,
            availability_domains,
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


def _sorted_rows(all_rows):
    return sorted(all_rows, key=lambda r: (r["region"], r["compartment_path"], r["type"], r["name"]))


def filter_by_min_age(rows, min_age_days):
    """Keep volumes whose age (Days from time_created) is >= min_age_days."""
    if min_age_days is None or min_age_days <= 0:
        return rows
    filtered = []
    for row in rows:
        try:
            if int(row["age"]) >= min_age_days:
                filtered.append(row)
        except (TypeError, ValueError):
            continue
    return filtered


def _export_records(rows):
    records = []
    for row in rows:
        records.append({key: row.get(key, "") for key, _ in EXPORT_COLUMNS})
    return records


def _ensure_export_extension(path, extension):
    """Append .csv / .xlsx when the user omits the extension."""
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
    ws.title = "Unattached volumes"
    ws.append([title for _, title in EXPORT_COLUMNS])
    for record in records:
        ws.append([record[key] for key, _ in EXPORT_COLUMNS])
    wb.save(path)


def parse_cmd():
    """Framework CLI plus script-only -csv / -excel / -minage (stripped before input_command_line)."""
    csv_path = ""
    xlsx_path = ""
    min_age_days = 0
    argv = sys.argv[1:]
    filtered = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-minage", "--minage"):
            if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                print("ERROR: -minage requires a minimum age in days (e.g. -minage 90)")
                sys.exit(1)
            try:
                min_age_days = int(argv[i + 1])
                if min_age_days < 0:
                    raise ValueError
            except ValueError:
                print("ERROR: -minage must be a non-negative integer (days)")
                sys.exit(1)
            i += 2
            continue
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
    cmd.min_age_days = min_age_days
    return cmd


def main():
    check_oci_version(MIN_OCI_VERSION)
    cmd = parse_cmd()

    writer = None
    if cmd.log_file:
        writer = MyWriter(sys.stdout, cmd.log_file)
        sys.stdout = writer

    try:
        print("Script: list_unattached_volumes.py  Version: {}".format(SCRIPT_VERSION))
        print("Listing boot and block volumes not attached to a compute instance...")
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
        compartment_count = len(scan_compartments)
        region_count = len(regions)
        progress = ScanProgress(compartment_count, region_count)

        all_rows = []
        region_errors = []

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    scan_region,
                    config,
                    signer,
                    tenancy_id,
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

        if cmd.min_age_days > 0:
            before = len(all_rows)
            all_rows = filter_by_min_age(all_rows, cmd.min_age_days)
            print("")
            print("Minimum age filter: >= {} days ({} of {} unattached volumes shown)".format(
                cmd.min_age_days, len(all_rows), before))

        print("")
        print("")

        header = "{:<18} {:<6} {:<28} {:<24} {:>8} {:>6} {:>8}  {:<24}".format(
            "Region", "Type", "Compartment", "Volume name", "SizeGB", "VPU", "Days", "Owner"
        )
        print(header)
        print("-" * len(header))

        sorted_rows = _sorted_rows(all_rows)
        for row in sorted_rows:
            print("{:<18} {:<6} {:<28} {:<24} {:>8} {:>6} {:>8}  {:<24}".format(
                row["region"],
                row["type"],
                _truncate(row["compartment_path"], 28),
                _truncate(row["name"], 24),
                row["size_gb"],
                _truncate(str(row["vpu"]), 6),
                row["age"],
                _truncate(row["owner"], 24),
            ))

        print("")
        total_size_gb = 0
        age_days = []
        for row in all_rows:
            try:
                total_size_gb += int(row["size_gb"])
            except (TypeError, ValueError):
                pass
            if row["age"] != "-":
                try:
                    age_days.append(int(row["age"]))
                except ValueError:
                    pass

        boot_count = sum(1 for r in all_rows if r["type"] == "boot")
        block_count = sum(1 for r in all_rows if r["type"] == "block")
        if age_days:
            avg_age = sum(age_days) / len(age_days)
            avg_age_text = "{:.1f}".format(avg_age)
        else:
            avg_age_text = "-"

        print("Total unattached volumes: {}".format(len(all_rows)))
        print("  boot: {}  block: {}".format(boot_count, block_count))
        print("  total size: {} GB".format(total_size_gb))
        print("  average age: {} days".format(avg_age_text))

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
