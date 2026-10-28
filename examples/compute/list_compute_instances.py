"""
List compute instances across subscribed regions with shape, OS, licensing, storage, and owner.

Output per instance: region, compartment, name, lifecycle, OS (from image), license status,
OCPUs, GPUs, memory (GB), boot volume size and VPU, attached block volume count and total size (GB),
owner (defined tags via get_owner), age in days (time_created).

One thread per region; progress updates per completed compartment
(compartments x regions = 100%).

IAM: read access to compute instances, volume attachments, block/boot volumes, and images
in compartments scanned.

Usage (from repository root):
    python examples/compute/list_compute_instances.py
    python examples/compute/list_compute_instances.py -rg eu-frankfurt-1
    python examples/compute/list_compute_instances.py -c /root/prod -log instances.txt
    python examples/compute/list_compute_instances.py -csv report
    python examples/compute/list_compute_instances.py -excel report

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
    resolve_export_paths,
    write_excel_export_file,
)

EXCEL_INT_COLUMNS = frozenset({
    "gpus",
    "block_volume_count",
    "block_volume_total_gb",
    "boot_size_gb",
    "boot_vpu",
    "age_days",
})
EXCEL_FLOAT_COLUMNS = frozenset({
    "ocpus",
    "memory_gb",
})

oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()

MIN_OCI_VERSION = "2.88.0"
SCRIPT_VERSION = "26.10.28"
DEFAULT_CSV_EXPORT = "compute_instances.csv"
DEFAULT_XLSX_EXPORT = "compute_instances.xlsx"

ATTACHED = oci.core.models.VolumeAttachment.LIFECYCLE_STATE_ATTACHED

EXPORT_COLUMNS = [
    ("region", "Region"),
    ("compartment_path", "Compartment"),
    ("instance_name", "Instance name"),
    ("lifecycle_state", "Lifecycle"),
    ("os", "OS"),
    ("license_status", "License status"),
    ("ocpus", "OCPUs"),
    ("gpus", "GPUs"),
    ("memory_gb", "Memory GB"),
    ("boot_size_gb", "Boot size GB"),
    ("boot_vpu", "Boot VPU"),
    ("block_volume_count", "Block volumes"),
    ("block_volume_total_gb", "Block total GB"),
    ("owner", "Owner"),
    ("age_days", "Days"),
    ("instance_id", "Instance OCID"),
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
            self._print_progress(region, compartment_path)

    def skip_remaining_compartments(self, region, count, reason=""):
        if count <= 0:
            return
        with self._lock:
            self.done += count
            label = reason if reason else "skipped"
            self._print_progress(region, label)

    def _print_progress(self, region, detail):
        pct = 100.0 * self.done / self.total if self.total else 100.0
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


def format_age_days(time_created):
    if time_created is None or not isinstance(time_created, datetime):
        return "-"
    dt = time_created
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days
    return str(max(days, 0))


def format_license_status(instance):
    configs = instance.licensing_configs or []
    if not configs:
        return "-"
    parts = []
    for cfg in configs:
        os_ver = getattr(cfg, "os_version", None) or ""
        base = "{}: {}".format(cfg.type or "?", cfg.license_type or "?")
        if os_ver:
            base = "{} ({})".format(base, os_ver)
        parts.append(base)
    return "; ".join(parts)


def shape_metric(value, fmt="{:.1f}"):
    if value is None:
        return "-"
    try:
        if isinstance(value, float) and value == int(value):
            return str(int(value))
        return fmt.format(value)
    except (TypeError, ValueError):
        return str(value)


def instance_shape_fields(instance):
    cfg = instance.shape_config
    if cfg is None:
        return "-", "-", "-"
    ocpus = shape_metric(cfg.ocpus)
    gpus = shape_metric(cfg.gpus, "{}")
    if gpus == "-":
        gpus = "0"
    memory = shape_metric(cfg.memory_in_gbs)
    return ocpus, gpus, memory


def vpu_display(volume):
    if volume is None:
        return "-"
    vpu = volume.vpus_per_gb
    if vpu is None and getattr(volume, "auto_tuned_vpus_per_gb", None) is not None:
        return str(volume.auto_tuned_vpus_per_gb) + " (auto)"
    if vpu is None:
        return "-"
    return str(vpu)


def resolve_os_label(compute, image_id, image_cache, region):
    if not image_id:
        return "-"
    if image_id in image_cache:
        return image_cache[image_id]

    def _call(strategy):
        return compute.get_image(image_id, retry_strategy=strategy).data

    try:
        image = call_with_manual_429_retry(_call, region)
        os_name = (image.operating_system or "").strip()
        os_ver = (image.operating_system_version or "").strip()
        if os_name and os_ver:
            label = "{} {}".format(os_name, os_ver)
        elif os_name:
            label = os_name
        elif os_ver:
            label = os_ver
        else:
            label = image.display_name or image_id
    except oci.exceptions.ServiceError:
        label = image_id

    image_cache[image_id] = label
    return label


def get_availability_domains(identity, tenancy_id, region):
    return paginate(
        region,
        identity.list_availability_domains,
        compartment_id=tenancy_id,
    )


def build_volume_caches(blockstorage, compartment_id, region):
    boot_by_id = {}
    block_by_id = {}
    try:
        for vol in paginate(region, blockstorage.list_boot_volumes, compartment_id=compartment_id):
            if vol.lifecycle_state not in ("TERMINATED", "TERMINATING"):
                boot_by_id[vol.id] = vol
    except oci.exceptions.ServiceError:
        pass
    try:
        for vol in paginate(region, blockstorage.list_volumes, compartment_id=compartment_id):
            if vol.lifecycle_state not in ("TERMINATED", "TERMINATING"):
                block_by_id[vol.id] = vol
    except oci.exceptions.ServiceError:
        pass
    return boot_by_id, block_by_id


def build_attachment_maps(compute, compartment_id, region, availability_domains):
    boot_by_instance = {}
    block_by_instance = {}

    try:
        for attachment in paginate(region, compute.list_volume_attachments, compartment_id=compartment_id):
            if attachment.lifecycle_state != ATTACHED or not attachment.instance_id:
                continue
            if not attachment.volume_id:
                continue
            block_by_instance.setdefault(attachment.instance_id, []).append(attachment.volume_id)
    except oci.exceptions.ServiceError:
        pass

    for ad in availability_domains:
        try:
            for attachment in paginate(
                region,
                compute.list_boot_volume_attachments,
                availability_domain=ad.name,
                compartment_id=compartment_id,
            ):
                if attachment.lifecycle_state != ATTACHED or not attachment.instance_id:
                    continue
                if attachment.boot_volume_id:
                    boot_by_instance[attachment.instance_id] = attachment.boot_volume_id
        except oci.exceptions.ServiceError:
            continue

    return boot_by_instance, block_by_instance


def boot_volume_stats(boot_volume_id, boot_by_id):
    if not boot_volume_id:
        return "-", "-"
    vol = boot_by_id.get(boot_volume_id)
    if vol is None:
        return "-", "-"
    size = vol.size_in_gbs if vol.size_in_gbs is not None else "-"
    return size, vpu_display(vol)


def block_volume_stats(volume_ids, block_by_id):
    if not volume_ids:
        return 0, 0
    total_gb = 0
    count = 0
    for vid in volume_ids:
        vol = block_by_id.get(vid)
        if vol is None:
            count += 1
            continue
        count += 1
        if vol.size_in_gbs is not None:
            total_gb += vol.size_in_gbs
    return count, total_gb


def instance_row(
    region,
    compartment_path,
    instance,
    boot_by_instance,
    block_by_instance,
    boot_by_id,
    block_by_id,
    os_label,
    config,
    config_profile,
):
    ocpus, gpus, memory = instance_shape_fields(instance)
    boot_id = boot_by_instance.get(instance.id)
    boot_size, boot_vpu = boot_volume_stats(boot_id, boot_by_id)
    block_ids = block_by_instance.get(instance.id, [])
    block_count, block_total_gb = block_volume_stats(block_ids, block_by_id)

    owner_info = get_owner(instance.defined_tags, config=config, config_profile=config_profile)

    return {
        "region": region,
        "compartment_path": compartment_path,
        "instance_name": instance.display_name or "",
        "lifecycle_state": instance.lifecycle_state or "-",
        "os": os_label,
        "license_status": format_license_status(instance),
        "ocpus": ocpus,
        "gpus": gpus,
        "memory_gb": memory,
        "boot_size_gb": boot_size,
        "boot_vpu": boot_vpu,
        "block_volume_count": block_count,
        "block_volume_total_gb": block_total_gb,
        "owner": owner_info.get("owner") or "-",
        "age_days": format_age_days(getattr(instance, "time_created", None)),
        "instance_id": instance.id,
    }


def scan_compartment(
    region,
    compute,
    blockstorage,
    availability_domains,
    compartment_id,
    compartment_path,
    config,
    config_profile,
    image_cache,
):
    rows = []
    errors = []

    try:
        instances = paginate(region, compute.list_instances, compartment_id=compartment_id)
    except oci.exceptions.ServiceError as e:
        if e.status in (403, 404):
            return rows, errors
        return rows, [(compartment_path, "list instances: {}".format(e.message))]

    boot_by_id, block_by_id = build_volume_caches(blockstorage, compartment_id, region)
    boot_by_instance, block_by_instance = build_attachment_maps(
        compute, compartment_id, region, availability_domains
    )

    for instance in instances:
        if instance.lifecycle_state == "TERMINATED":
            continue
        os_label = resolve_os_label(compute, instance.image_id, image_cache, region)
        rows.append(
            instance_row(
                region,
                compartment_path,
                instance,
                boot_by_instance,
                block_by_instance,
                boot_by_id,
                block_by_id,
                os_label,
                config,
                config_profile,
            )
        )

    return rows, errors


def scan_region(
    config,
    signer,
    tenancy_id,
    region,
    compartments_to_check,
    config_profile,
    progress,
):
    regional_config = dict(config)
    regional_config["region"] = region
    compute = oci.core.ComputeClient(regional_config, signer=signer)
    blockstorage = oci.core.BlockstorageClient(regional_config, signer=signer)
    identity = oci.identity.IdentityClient(regional_config, signer=signer)
    image_cache = {}

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
            compute,
            blockstorage,
            availability_domains,
            comp.details.id,
            comp.fullpath,
            config,
            config_profile,
            image_cache,
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
        key=lambda r: (r["region"], r["compartment_path"], r["instance_name"]),
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
        sheet_title="Compute instances",
        int_columns=EXCEL_INT_COLUMNS,
        float_columns=EXCEL_FLOAT_COLUMNS,
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
        print("Script: list_compute_instances.py  Version: {}".format(SCRIPT_VERSION))
        print("Listing compute instances (all subscribed regions unless -rg is set)...")
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

        print("")
        print("")

        sorted_rows = _sorted_rows(all_rows)
        header = (
            "{:<16} {:<24} {:<22} {:<10} {:<18} {:>5} {:>4} {:>6} {:>6} {:>6} {:>6} {:>8}"
        ).format(
            "Region",
            "Compartment",
            "Instance",
            "Lifecycle",
            "OS",
            "OCPU",
            "GPU",
            "MemGB",
            "BootGB",
            "Blk#",
            "BlkGB",
            "Days",
        )
        print(header)
        print("-" * len(header))

        for row in sorted_rows:
            print(
                "{:<16} {:<24} {:<22} {:<10} {:<18} {:>5} {:>4} {:>6} {:>6} {:>6} {:>6} {:>8}".format(
                    row["region"],
                    _truncate(row["compartment_path"], 24),
                    _truncate(row["instance_name"], 22),
                    _truncate(row["lifecycle_state"], 10),
                    _truncate(row["os"], 18),
                    _truncate(str(row["ocpus"]), 5),
                    _truncate(str(row["gpus"]), 4),
                    _truncate(str(row["memory_gb"]), 6),
                    _truncate(str(row["boot_size_gb"]), 6),
                    row["block_volume_count"],
                    row["block_volume_total_gb"],
                    row["age_days"],
                )
            )

        print("")
        running = sum(1 for r in sorted_rows if r["lifecycle_state"] == "RUNNING")
        total_ocpus = 0.0
        total_mem = 0.0
        for row in sorted_rows:
            try:
                if row["ocpus"] != "-":
                    total_ocpus += float(row["ocpus"])
            except ValueError:
                pass
            try:
                if row["memory_gb"] != "-":
                    total_mem += float(row["memory_gb"])
            except ValueError:
                pass

        print("Total instances: {}  (RUNNING: {})".format(len(sorted_rows), running))
        print("  sum OCPUs (where reported): {:.1f}".format(total_ocpus))
        print("  sum memory GB (where reported): {:.1f}".format(total_mem))
        print("")
        print("License status and boot VPU are included in CSV/Excel export columns.")

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
