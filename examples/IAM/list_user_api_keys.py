"""
List IAM users in the tenancy and each user's API signing keys with creation time.

IAM users and API keys are tenancy-wide resources. The script uses the tenancy home
region Identity endpoint (or -rg if you pass a subscribed region).

Users are listed from every ACTIVE identity domain in the tenancy (Identity Domains API),
with legacy IAM-only users added when they are not returned from any domain.
One row per API key. Users with no API keys appear once with key fields set to "-".

Does not print or export public key material (key_value), only fingerprint and metadata.

IAM: inspect users, inspect user-api-keys, and (for domain names) inspect domains and
read users in identity domains via the Identity Domains API.

Usage (from repository root):
    python examples/IAM/list_user_api_keys.py
    python examples/IAM/list_user_api_keys.py -log user_api_keys.txt
    python examples/IAM/list_user_api_keys.py -csv report
    python examples/IAM/list_user_api_keys.py -excel report

Excel export requires: pip install openpyxl
"""

import csv
import os
import sys
import time
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
    get_owner,
    resolve_export_paths,
    write_excel_export_file,
)

oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()

MIN_OCI_VERSION = "2.88.0"
SCRIPT_VERSION = "26.11.02"
DEFAULT_CSV_EXPORT = "user_api_keys.csv"
DEFAULT_XLSX_EXPORT = "user_api_keys.xlsx"

DEFAULT_IAM_DOMAIN_LABEL = "Default IAM"

EXPORT_COLUMNS = [
    ("user_name", "User name"),
    ("user_email", "Email"),
    ("identity_domain", "Identity domain"),
    ("user_state", "User state"),
    ("api_key_fingerprint", "API key fingerprint"),
    ("api_key_state", "Key state"),
    ("key_created", "Key created (UTC)"),
    ("age_days", "Key age days"),
    ("owner", "Owner"),
    ("user_id", "User OCID"),
]

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

EXCEL_INT_COLUMNS = frozenset({"age_days"})


def call_with_manual_429_retry(operation):
    last_error = None
    for attempt in range(MANUAL_429_MAX_ATTEMPTS):
        try:
            return operation(THROTTLE_RETRY_STRATEGY if attempt > 0 else oci.retry.DEFAULT_RETRY_STRATEGY)
        except oci.exceptions.ServiceError as e:
            last_error = e
            if e.status == 429 and attempt < MANUAL_429_MAX_ATTEMPTS - 1:
                time.sleep(min(120, MANUAL_429_BASE_SLEEP * (2 ** attempt)))
                continue
            raise
    if last_error:
        raise last_error


def paginate(list_func, **kwargs):
    def _call(strategy):
        return oci.pagination.list_call_get_all_results(
            list_func, retry_strategy=strategy, **kwargs
        ).data

    return call_with_manual_429_retry(_call)


def format_key_created(time_created):
    if time_created is None or not isinstance(time_created, datetime):
        return "-"
    dt = time_created
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def key_age_days(time_created):
    if time_created is None or not isinstance(time_created, datetime):
        return "-"
    dt = time_created
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days
    return str(max(days, 0))


def normalize_domain_endpoint(url):
    if not url:
        return None
    endpoint = url.strip().rstrip("/")
    if not endpoint.startswith("http"):
        endpoint = "https://" + endpoint
    return endpoint


def list_idcs_users(idcs_client):
    """Paginate Identity Domains SCIM list_users."""
    resources = []
    start_index = 1
    page_size = 500
    while True:
        def _call(strategy, start=start_index):
            return idcs_client.list_users(
                count=page_size,
                start_index=start,
                retry_strategy=strategy,
            )

        response = call_with_manual_429_retry(_call)
        data = response.data
        chunk = data.resources or []
        resources.extend(chunk)
        total = data.total_results
        if not chunk:
            break
        if total is not None and start_index + len(chunk) > total:
            break
        if len(chunk) < page_size:
            break
        start_index += page_size
    return resources


def list_identity_domains(identity, tenancy_id, errors):
    try:
        return paginate(identity.list_domains, compartment_id=tenancy_id)
    except oci.exceptions.ServiceError as e:
        if e.status not in (403, 404):
            errors.append(("identity domains", "list domains: {}".format(e.message)))
        return []


def domain_client_config(base_config, domain):
    cfg = dict(base_config)
    home = getattr(domain, "home_region", None) or ""
    if home:
        cfg["region"] = home
    return cfg


def domain_endpoint_candidates(domain):
    seen = set()
    for raw in (domain.url, domain.home_region_url):
        endpoint = normalize_domain_endpoint(raw)
        if endpoint and endpoint not in seen:
            seen.add(endpoint)
            yield endpoint


def idcs_primary_email(idcs_user):
    emails = idcs_user.emails or []
    for entry in emails:
        if getattr(entry, "primary", False) and getattr(entry, "value", None):
            return entry.value
    for entry in emails:
        if getattr(entry, "value", None):
            return entry.value
    return "-"


def idcs_display_name(idcs_user):
    if idcs_user.display_name:
        return idcs_user.display_name
    if idcs_user.user_name:
        return idcs_user.user_name
    return idcs_user.ocid or idcs_user.id or "-"


def idcs_lifecycle_label(idcs_user):
    if idcs_user.active is True:
        return "ACTIVE"
    if idcs_user.active is False:
        return "INACTIVE"
    return "-"


def iam_user_display_name(iam_user):
    if iam_user.description:
        return iam_user.description
    if iam_user.name:
        return iam_user.name
    return iam_user.id or "-"


def fetch_defined_tags(identity, user_ocid, cache):
    if not user_ocid:
        return {}
    if user_ocid in cache:
        return cache[user_ocid]
    try:
        iam_user = call_with_manual_429_retry(
            lambda strategy: identity.get_user(user_ocid, retry_strategy=strategy).data
        )
        cache[user_ocid] = iam_user.defined_tags or {}
    except oci.exceptions.ServiceError:
        cache[user_ocid] = {}
    return cache[user_ocid]


def user_record_from_idcs(idcs_user, domain_label, defined_tags_cache, identity):
    iam_ocid = idcs_user.ocid or ""
    return {
        "user_id": iam_ocid or idcs_user.id or "-",
        "api_key_user_id": iam_ocid,
        "user_name": idcs_display_name(idcs_user),
        "user_email": idcs_primary_email(idcs_user),
        "identity_domain": domain_label,
        "user_state": idcs_lifecycle_label(idcs_user),
        "defined_tags": fetch_defined_tags(identity, iam_ocid, defined_tags_cache)
        if iam_ocid
        else {},
    }


def user_record_from_iam(iam_user, domain_label):
    return {
        "user_id": iam_user.id or "-",
        "api_key_user_id": iam_user.id or "",
        "user_name": iam_user_display_name(iam_user),
        "user_email": iam_user.email or "-",
        "identity_domain": domain_label,
        "user_state": iam_user.lifecycle_state or "-",
        "defined_tags": iam_user.defined_tags or {},
    }


def collect_user_records(identity, regional_config, signer, tenancy_id, errors):
    """Build user list from all identity domains, then add IAM-only users."""
    records = []
    seen_keys = set()
    domains = list_identity_domains(identity, tenancy_id, errors)
    defined_tags_cache = {}

    for domain in domains:
        if domain.lifecycle_state != "ACTIVE":
            continue
        domain_label = domain.display_name or domain.id or "-"
        domain_config = domain_client_config(regional_config, domain)
        endpoints = list(domain_endpoint_candidates(domain))
        if not endpoints:
            errors.append((domain_label, "domain has no URL; skipped"))
            continue

        idcs_users = None
        last_error = None
        for endpoint in endpoints:
            try:
                idcs = oci.identity_domains.IdentityDomainsClient(
                    domain_config,
                    service_endpoint=endpoint,
                    signer=signer,
                )
                idcs_users = list_idcs_users(idcs)
                break
            except oci.exceptions.ServiceError as e:
                last_error = e
        if idcs_users is None:
            if last_error and last_error.status not in (403, 404):
                errors.append((domain_label, "list domain users: {}".format(last_error.message)))
            continue

        for idcs_user in idcs_users:
            dedupe_key = idcs_user.ocid or idcs_user.id
            if not dedupe_key or dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            records.append(
                user_record_from_idcs(
                    idcs_user, domain_label, defined_tags_cache, identity
                )
            )

    try:
        iam_users = paginate(identity.list_users, compartment_id=tenancy_id)
    except oci.exceptions.ServiceError as e:
        errors.append(("tenancy", "list IAM users: {}".format(e.message)))
        iam_users = []

    for iam_user in iam_users:
        if iam_user.lifecycle_state == "DELETED":
            continue
        if iam_user.id in seen_keys:
            continue
        seen_keys.add(iam_user.id)
        if iam_user.identity_provider_id:
            domain_label = "Federated"
        else:
            domain_label = DEFAULT_IAM_DOMAIN_LABEL
        records.append(user_record_from_iam(iam_user, domain_label))

    return records, len([d for d in domains if d.lifecycle_state == "ACTIVE"])


def rows_for_user_record(user_record, api_keys, config, config_profile):
    owner_info = get_owner(
        user_record.get("defined_tags"),
        config=config,
        config_profile=config_profile,
    )
    owner = owner_info.get("owner") or "-"
    base = {
        "user_name": user_record["user_name"],
        "user_email": user_record["user_email"],
        "identity_domain": user_record["identity_domain"],
        "user_state": user_record["user_state"],
        "owner": owner,
        "user_id": user_record["user_id"],
    }

    if not api_keys:
        return [
            dict(
                base,
                api_key_fingerprint="-",
                api_key_state="-",
                key_created="-",
                age_days="-",
            )
        ]

    rows = []
    for key in sorted(api_keys, key=lambda k: k.time_created or datetime.min.replace(tzinfo=timezone.utc)):
        if key.lifecycle_state == "DELETED":
            continue
        rows.append(
            dict(
                base,
                api_key_fingerprint=key.fingerprint or key.key_id or "-",
                api_key_state=key.lifecycle_state or "-",
                key_created=format_key_created(key.time_created),
                age_days=key_age_days(key.time_created),
            )
        )
    if not rows:
        return [
            dict(
                base,
                api_key_fingerprint="-",
                api_key_state="-",
                key_created="-",
                age_days="-",
            )
        ]
    return rows


def collect_user_api_key_rows(
    identity,
    regional_config,
    signer,
    tenancy_id,
    config,
    config_profile,
    errors,
):
    rows = []
    user_records, active_domain_count = collect_user_records(
        identity, regional_config, signer, tenancy_id, errors
    )

    for user_record in sorted(user_records, key=lambda r: (r["identity_domain"].lower(), r["user_name"].lower())):
        api_key_user_id = user_record.get("api_key_user_id") or ""
        api_keys = []
        if api_key_user_id:
            try:
                api_keys = paginate(identity.list_api_keys, user_id=api_key_user_id)
            except oci.exceptions.ServiceError as e:
                if e.status not in (403, 404):
                    errors.append(
                        (user_record["user_name"], "list api keys: {}".format(e.message))
                    )
        rows.extend(rows_for_user_record(user_record, api_keys, config, config_profile))

    return rows, active_domain_count, len(user_records)


def identity_region(config, signer, cmd):
    home = GetHomeRegion(config, signer) or config.get("region")
    if cmd.region:
        subscribed = SubscribedRegions(config, signer)
        if cmd.region not in subscribed:
            print("Region '{}' is not subscribed in this tenancy, aborting".format(cmd.region))
            sys.exit(1)
        return cmd.region
    return home


def _sorted_rows(all_rows):
    return sorted(
        all_rows,
        key=lambda r: (r["identity_domain"].lower(), r["user_name"].lower(), r["key_created"]),
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
        sheet_title="User API keys",
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
        print("Script: list_user_api_keys.py  Version: {}".format(SCRIPT_VERSION))
        print("Listing IAM users and API signing keys...")
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

        region = identity_region(config, signer, cmd)
        regional_config = dict(config)
        regional_config["region"] = region
        print("Identity API region: {}".format(region))
        print("")

        identity = oci.identity.IdentityClient(regional_config, signer=signer)
        errors = []
        all_rows, active_domain_count, user_count = collect_user_api_key_rows(
            identity,
            regional_config,
            signer,
            tenancy_id,
            config,
            cmd.config_profile,
            errors,
        )
        print("Identity domains scanned (ACTIVE): {}".format(active_domain_count))
        print("Users collected: {}".format(user_count))
        print("")

        sorted_rows = _sorted_rows(all_rows)
        header = "{:<24} {:<28} {:<20} {:<10} {:<40} {:>8}".format(
            "User name",
            "Email",
            "Identity domain",
            "Key state",
            "API key fingerprint",
            "Days",
        )
        print(header)
        print("-" * len(header))

        for row in sorted_rows:
            print(
                "{:<24} {:<28} {:<20} {:<10} {:<40} {:>8}".format(
                    _truncate(row["user_name"], 24),
                    _truncate(row["user_email"], 28),
                    _truncate(row["identity_domain"], 20),
                    _truncate(row["api_key_state"], 10),
                    _truncate(row["api_key_fingerprint"], 40),
                    row["age_days"],
                )
            )

        print("")
        users_with_keys = {r["user_id"] for r in sorted_rows if r["api_key_fingerprint"] != "-"}
        key_rows = sum(1 for r in sorted_rows if r["api_key_fingerprint"] != "-")
        print("Total rows: {}  (users with at least one API key: {})".format(len(sorted_rows), len(users_with_keys)))
        print("  API key rows: {}".format(key_rows))

        if cmd.csv_export:
            write_csv_export(cmd.csv_export, sorted_rows)
            print("CSV export written: {}".format(os.path.abspath(cmd.csv_export)))
        if cmd.excel_export:
            write_excel_export(cmd.excel_export, sorted_rows)
            print("Excel export written: {}".format(os.path.abspath(cmd.excel_export)))

        if errors:
            print("Non-fatal errors (first 10):")
            for path, message in errors[:10]:
                print("  {}  {}".format(path, message))
            if len(errors) > 10:
                print("  ... and {} more".format(len(errors) - 10))

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
