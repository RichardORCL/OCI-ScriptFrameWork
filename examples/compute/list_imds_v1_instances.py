"""
List compute instances that still allow IMDS version 1 (legacy /opc/v1 endpoints).

Oracle documents this on InstanceOptions.areLegacyImdsEndpointsDisabled:
  - false (default) -> IMDS v1 and v2 are enabled
  - true            -> v1 disabled (v2 only)

See: https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/gettingmetadata.htm

Scans every subscribed region unless -rg selects one region. Uses one worker
thread per region (OCI throttling is per region). Within each region, work is
sequential; the first 429 in a region switches that region to THROTTLE_RETRY.

Usage (from repository root):
    python examples/compute/list_imds_v1_instances.py
    python examples/compute/list_imds_v1_instances.py -cp DEFAULT -log imds_v1.txt
    python examples/compute/list_imds_v1_instances.py -c /root/prod
    python examples/compute/list_imds_v1_instances.py -disable   # set IMDS v2-only on matches

With -disable, each matching instance is updated (UpdateInstance, areLegacyImdsEndpointsDisabled=true).
Ensure workloads use IMDSv2 before disabling v1. Requires permission to manage instances.
"""

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import oci
from oci.retry import RetryStrategyBuilder

# Allow imports from repository root when run as a script
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
)

oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()

MIN_OCI_VERSION = "2.88.0"
SCRIPT_VERSION = "26.10.28"

# Per-region: start with DEFAULT_RETRY; after first 429, use THROTTLE_RETRY for that region only
_region_throttle_active = {}
_region_throttle_lock = threading.Lock()

# Retries 429 / transient 5xx with backoff (used once a region is throttled)
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


def mark_region_throttled(region):
    with _region_throttle_lock:
        _region_throttle_active[region] = True


def retry_strategy_for_region(region):
    with _region_throttle_lock:
        if _region_throttle_active.get(region):
            return THROTTLE_RETRY_STRATEGY
    return oci.retry.DEFAULT_RETRY_STRATEGY


def region_worker_count(regions):
    """One thread per region — matches OCI per-region throttle boundaries."""
    return max(1, len(regions))


def imds_v1_enabled(instance):
    """
    Return True if legacy IMDSv1 endpoints are still allowed for this instance.
    Default (unset) is v1 enabled per OCI API documentation.
    """
    options = instance.instance_options
    if options is None or options.are_legacy_imds_endpoints_disabled is None:
        return True
    return not options.are_legacy_imds_endpoints_disabled


def imds_setting_label(instance):
    if imds_v1_enabled(instance):
        return "v1+v2 (legacy v1 enabled)"
    return "v2 only"


def compartments_to_scan(all_compartments, selected_compartment_id):
    if not selected_compartment_id:
        return all_compartments
    return [
        c for c in all_compartments
        if _instance_in_compartment_subtree(c.details.id, selected_compartment_id, all_compartments)
    ]


def call_with_manual_429_retry(operation, region):
    """Extra backoff for throttling; enables THROTTLE_RETRY_STRATEGY for this region after 429."""
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
                    sleep_secs = min(120, MANUAL_429_BASE_SLEEP * (2 ** attempt))
                    time.sleep(sleep_secs)
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


def scan_region(config, signer, region, compartments_to_check):
    """List instances in every compartment for one region (runs in a worker thread)."""
    regional_config = dict(config)
    regional_config["region"] = region
    compute = oci.core.ComputeClient(regional_config, signer=signer)
    instances = []
    errors = []
    for comp in compartments_to_check:
        try:
            instances.extend(list_instances_in_compartment(compute, comp.details.id, region))
        except oci.exceptions.ServiceError as e:
            if e.status in (404, 403):
                continue
            if e.status == 429:
                errors.append((comp.fullpath, "{} (retries exhausted)".format(e.message)))
                continue
            errors.append((comp.fullpath, e.message))
    return region, instances, errors


def collect_imds_v1_matches(region, instances, compartment_paths):
    rows = []
    for instance in instances:
        if instance.lifecycle_state == "TERMINATED":
            continue
        if not imds_v1_enabled(instance):
            continue
        comp_path = compartment_paths.get(instance.compartment_id, instance.compartment_id)
        rows.append({
            "region": region,
            "compartment_path": comp_path,
            "compartment_id": instance.compartment_id,
            "display_name": instance.display_name or "",
            "instance_id": instance.id,
            "lifecycle_state": instance.lifecycle_state,
            "imds": imds_setting_label(instance),
        })
    return rows


def update_instance_imds_v2_only(compute, instance_id, region):
    update_details = oci.core.models.UpdateInstanceDetails(
        instance_options=oci.core.models.InstanceOptions(
            are_legacy_imds_endpoints_disabled=True
        )
    )
    call_with_manual_429_retry(
        lambda strategy: compute.update_instance(
            instance_id=instance_id,
            update_instance_details=update_details,
            retry_strategy=strategy,
        ),
        region,
    )


def disable_instances_in_region(config, signer, region, rows):
    """Update all instances in one region sequentially (same regional throttle bucket)."""
    regional_config = dict(config)
    regional_config["region"] = region
    compute = oci.core.ComputeClient(regional_config, signer=signer)
    ok = []
    failed = []
    for row in rows:
        try:
            update_instance_imds_v2_only(compute, row["instance_id"], region)
            ok.append(row)
        except oci.exceptions.ServiceError as e:
            failed.append((row, e.message))
        except Exception as e:
            failed.append((row, str(e)))
    return ok, failed


def _is_throttle_error(message):
    if not message:
        return False
    lower = message.lower()
    return "too many requests" in lower or "429" in lower


def apply_disable_imds_v1(config, signer, matches):
    if not matches:
        print("No instances to update.")
        return [], []

    by_region = {}
    for row in matches:
        by_region.setdefault(row["region"], []).append(row)
    regions = sorted(by_region.keys())
    workers = region_worker_count(regions)

    print("")
    print("WARNING: -disable updates {} instance(s) to IMDS v2-only (legacy v1 disabled).".format(len(matches)))
    print("Parallel region workers (one per region): {}".format(workers))
    print("")

    ok = []
    failed = []
    completed_regions = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(disable_instances_in_region, config, signer, region, by_region[region]): region
            for region in regions
        }
        for future in as_completed(futures):
            region = futures[future]
            region_ok, region_failed = future.result()
            ok.extend(region_ok)
            failed.extend(region_failed)
            completed_regions += 1
            print("Update regions completed: {}/{} (latest: {}, {} ok)".format(
                completed_regions, len(regions), region, len(region_ok)), end="\r")

    throttle_rows = [row for row, err in failed if _is_throttle_error(err)]
    other_failed = [(row, err) for row, err in failed if not _is_throttle_error(err)]

    if throttle_rows:
        print("")
        print("Retrying {} throttled update(s) in-region with backoff...".format(len(throttle_rows)))
        retry_by_region = {}
        for row in throttle_rows:
            retry_by_region.setdefault(row["region"], []).append(row)
        for region in sorted(retry_by_region.keys()):
            region_ok, region_failed = disable_instances_in_region(
                config, signer, region, retry_by_region[region]
            )
            ok.extend(region_ok)
            other_failed.extend(region_failed)

    print("")
    return ok, other_failed


def parse_cmd():
    """Framework CLI plus script-only -disable (not passed to shared input_command_line)."""
    disable_imds_v1 = "-disable" in sys.argv[1:] or "--disable" in sys.argv[1:]
    if disable_imds_v1:
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a not in ("-disable", "--disable")]
    cmd = input_command_line()
    cmd.disable_imds_v1 = disable_imds_v1
    return cmd


def main():
    check_oci_version(MIN_OCI_VERSION)
    cmd = parse_cmd()

    writer = None
    if cmd.log_file:
        writer = MyWriter(sys.stdout, cmd.log_file)
        sys.stdout = writer

    try:
        print("Script: list_imds_v1_instances.py  Version: {}".format(SCRIPT_VERSION))
        print("Finding instances with IMDS v1 enabled (areLegacyImdsEndpointsDisabled=false)...")
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
        workers = region_worker_count(regions)
        print("Compartments to scan per region: {}".format(len(scan_compartments)))
        print("Parallel region workers (one per region): {}".format(workers))
        print("")

        matches = []
        region_errors = []
        seen_instance_ids = set()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(scan_region, config, signer, region, scan_compartments): region
                for region in regions
            }
            completed = 0
            for future in as_completed(futures):
                region, instances, comp_errors = future.result()
                completed += 1
                print("Regions completed: {}/{} (latest: {})".format(completed, len(regions), region), end="\r")

                for comp_path, message in comp_errors:
                    region_errors.append((region, comp_path, message))

                for row in collect_imds_v1_matches(region, instances, compartment_paths):
                    if row["instance_id"] in seen_instance_ids:
                        continue
                    seen_instance_ids.add(row["instance_id"])
                    matches.append(row)

        print("")

        header = "{:<18} {:<36} {:<30} {:<12} {:<24}".format(
            "Region", "Compartment", "Display name", "State", "Instance OCID"
        )
        print(header)
        print("-" * len(header))

        for row in sorted(matches, key=lambda r: (r["region"], r["compartment_path"], r["display_name"])):
            print("{:<18} {:<36} {:<30} {:<12} {:<24}".format(
                row["region"],
                _truncate(row["compartment_path"], 36),
                _truncate(row["display_name"], 30),
                row["lifecycle_state"],
                row["instance_id"],
            ))

        print("")
        print("Total instances with IMDS v1 enabled: {}".format(len(matches)))
        if region_errors:
            print("Non-fatal list errors (first 10):")
            for region, comp_path, message in region_errors[:10]:
                print("  {}  {}  {}".format(region, comp_path, message))
            if len(region_errors) > 10:
                print("  ... and {} more".format(len(region_errors) - 10))

        by_region = {}
        for row in matches:
            by_region[row["region"]] = by_region.get(row["region"], 0) + 1
        if by_region:
            print("By region:")
            for region in sorted(by_region):
                print("  {}: {}".format(region, by_region[region]))

        if cmd.disable_imds_v1:
            ok, failed = apply_disable_imds_v1(config, signer, matches)
            print("Updated to IMDS v2-only: {}".format(len(ok)))
            if failed:
                print("Update failures: {}".format(len(failed)))
                for row, message in failed[:20]:
                    print("  {}  {}  {}".format(row["region"], row["display_name"], message))
                if len(failed) > 20:
                    print("  ... and {} more".format(len(failed) - 20))
                sys.exit(1)

    finally:
        if writer is not None:
            sys.stdout = writer.stdout
            writer.close()


def _truncate(text, max_len):
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _instance_in_compartment_subtree(instance_compartment_id, selected_compartment_id, compartments):
    """True if instance compartment is the selected compartment or a descendant."""
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
