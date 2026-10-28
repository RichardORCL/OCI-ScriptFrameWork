"""
List RUNNING compute instances with Windows and OCI-provided license (license included).

Matches instances where licensingConfigs contains type=WINDOWS and
licenseType=OCI_PROVIDED (Windows Server license included / metered by OCI).

Output: region, compartment path, instance name, lifecycle state, OCPU count, owner, age (days).

One thread per region; progress updates per completed compartment
(compartments x regions = 100%).

Usage (from repository root):
    python examples/compute/list_windows_license_included.py
    python examples/compute/list_windows_license_included.py -rg eu-frankfurt-1
    python examples/compute/list_windows_license_included.py -c /root/prod -log win.txt
"""

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

LC = oci.core.models.LicensingConfig


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


def list_instances_in_compartment(compute, compartment_id, region):
    return call_with_manual_429_retry(
        lambda strategy: oci.pagination.list_call_get_all_results(
            compute.list_instances,
            compartment_id=compartment_id,
            retry_strategy=strategy,
        ).data,
        region,
    )


def is_windows_license_included(instance):
    if instance.lifecycle_state != "RUNNING":
        return False
    for cfg in instance.licensing_configs or []:
        if cfg.type == LC.TYPE_WINDOWS and cfg.license_type == LC.LICENSE_TYPE_OCI_PROVIDED:
            return True
    return False


def instance_ocpus(instance):
    if instance.shape_config is not None and instance.shape_config.ocpus is not None:
        return instance.shape_config.ocpus
    return "-"


def format_instance_age_days(time_created):
    if time_created is None or not isinstance(time_created, datetime):
        return "-"
    dt = time_created if time_created.tzinfo else time_created.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days
    return str(max(days, 0))


def compartments_to_scan(all_compartments, selected_compartment_id):
    if not selected_compartment_id:
        return all_compartments
    return [
        c for c in all_compartments
        if _instance_in_compartment_subtree(c.details.id, selected_compartment_id, all_compartments)
    ]


def collect_matches(region, instances, compartment_paths, config, config_profile):
    rows = []
    for instance in instances:
        if not is_windows_license_included(instance):
            continue
        comp_path = compartment_paths.get(instance.compartment_id, instance.compartment_id)
        owner_info = get_owner(instance.defined_tags, config=config, config_profile=config_profile)
        rows.append({
            "region": region,
            "compartment_path": comp_path,
            "name": instance.display_name or "",
            "lifecycle_state": instance.lifecycle_state or "-",
            "ocpus": instance_ocpus(instance),
            "owner": owner_info.get("owner") or "-",
            "age": format_instance_age_days(getattr(instance, "time_created", None)),
            "instance_id": instance.id,
        })
    return rows


def scan_region(config, signer, region, compartments_to_check, compartment_paths, config_profile, progress):
    regional_config = dict(config)
    regional_config["region"] = region
    compute = oci.core.ComputeClient(regional_config, signer=signer)
    rows = []
    errors = []
    for comp in compartments_to_check:
        try:
            instances = list_instances_in_compartment(compute, comp.details.id, region)
            rows.extend(collect_matches(region, instances, compartment_paths, config, config_profile))
        except oci.exceptions.ServiceError as e:
            if e.status not in (403, 404):
                errors.append((comp.fullpath, e.message))
        progress.compartment_finished(region, comp.fullpath)
    return region, rows, errors


def main():
    check_oci_version(MIN_OCI_VERSION)
    cmd = input_command_line()

    writer = None
    if cmd.log_file:
        writer = MyWriter(sys.stdout, cmd.log_file)
        sys.stdout = writer

    try:
        print("Script: list_windows_license_included.py  Version: {}".format(SCRIPT_VERSION))
        print("Listing RUNNING Windows instances with OCI-provided (included) license...")
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
        compartment_paths = {c.details.id: c.fullpath for c in compartments}

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
                    compartment_paths,
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

        sorted_rows = sorted(all_rows, key=lambda r: (r["region"], r["compartment_path"], r["name"]))
        header = "{:<18} {:<32} {:<24} {:<12} {:>6} {:>8}  {:<24}".format(
            "Region", "Compartment", "Instance name", "Lifecycle", "OCPUs", "Days", "Owner"
        )
        print(header)
        print("-" * len(header))
        for row in sorted_rows:
            print("{:<18} {:<32} {:<24} {:<12} {:>6} {:>8}  {:<24}".format(
                row["region"],
                _truncate(row["compartment_path"], 32),
                _truncate(row["name"], 24),
                row["lifecycle_state"],
                row["ocpus"],
                row["age"],
                _truncate(row["owner"], 24),
            ))

        print("")
        print("Total matching instances: {}".format(len(sorted_rows)))
        total_ocpus = 0
        age_days = []
        for row in sorted_rows:
            try:
                total_ocpus += float(row["ocpus"])
            except (TypeError, ValueError):
                pass
            if row["age"] != "-":
                try:
                    age_days.append(int(row["age"]))
                except ValueError:
                    pass
        print("  total OCPUs: {}".format(int(total_ocpus) if total_ocpus == int(total_ocpus) else total_ocpus))
        if age_days:
            print("  average age: {:.1f} days".format(sum(age_days) / len(age_days)))
        else:
            print("  average age: -")

        if region_errors:
            print("Non-fatal errors (first 10):")
            for region, path, message in region_errors[:10]:
                print("  {}  {}  {}".format(region, path, message))

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
