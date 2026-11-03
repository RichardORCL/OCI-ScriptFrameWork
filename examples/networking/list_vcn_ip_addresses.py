"""
List all IP addresses in use in one OCI Virtual Cloud Network (VCN).

The VCN is selected with -vcn, either by OCID or by display name:
  * OCID: the VCN is looked up directly (in -rg, or in each subscribed region until found).
  * Name: the display name is looked up with OCI Resource Search (one structured query
    per subscribed region, or only -rg), optionally limited to the -c compartment subtree.
    Newly created or renamed VCNs can take a short while to appear in the search index. If the name matches more than one
    VCN, the matches are printed with their OCIDs and the script stops; re-run with the
    OCID, or narrow the search with -c / -rg.

For the selected VCN the script reports, one row per IP address:
  * Private IPv4 addresses in every subnet (primary and secondary IPs on VNICs, and
    reserved private IPs), with hostname, VNIC name and the instance the VNIC is attached to.
  * The public IPv4 address (ephemeral or reserved) assigned to each private IP, if any.
  * IPv6 addresses in every subnet.
  * Private IPv4 addresses in every VLAN of the VCN (Oracle Cloud VMware Solution), plus
    any public (external access) IP assigned to them; its name is shown in "Attached to".
  * NAT gateway public IPs of the VCN.

Subnets, VNICs and gateways of a VCN can live in other compartments than the VCN itself,
so resources of the VCN (subnets, VLANs, NAT gateways) are collected from all compartments you can read (-c only narrows
the VCN name search). IPs on service-managed VNICs (for example Load Balancers or some
database services) are only reported when the Networking API returns them for the subnet.
For every subnet and VLAN an extra row is added for the first usable IP of its CIDR range
(e.g. 10.100.0.1 in 10.100.0.0/24), the default GATEWAY/DNS address OCI reserves there.
OCI's other reserved addresses (the network and broadcast address) are not listed.

Minimum IAM (read only), for example:
    Allow group <group> to inspect compartments in tenancy
    Allow group <group> to read virtual-network-family in tenancy   (also used by Resource Search)
    Allow group <group> to read instance-family in tenancy

Usage (from repository root):
    python examples/networking/list_vcn_ip_addresses.py -vcn ocid1.vcn.oc1.eu-frankfurt-1.aaaa...
    python examples/networking/list_vcn_ip_addresses.py -vcn my-vcn
    python examples/networking/list_vcn_ip_addresses.py -vcn my-vcn -c /root/prod -rg eu-frankfurt-1
    python examples/networking/list_vcn_ip_addresses.py -vcn my-vcn -log vcn_ips.txt
    python examples/networking/list_vcn_ip_addresses.py -vcn my-vcn -csv report
    python examples/networking/list_vcn_ip_addresses.py -vcn my-vcn -excel report

CSV export writes two files: <name>_segments.csv (one row per subnet / VLAN of the VCN) and
<name>_ips.csv (the IP address listing), e.g. -csv report -> report_segments.csv, report_ips.csv.
Excel export writes one file with the same two listings as sheets "Subnets and VLANs" and
"IP addresses".

Excel export requires: pip install openpyxl
"""

import csv
import ipaddress
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    GetCompartmentFullPath,
    FindCompartment,
    resolve_export_paths,
    write_excel_export_sheets,
)

oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()

MIN_OCI_VERSION = "2.88.0"
SCRIPT_VERSION = "26.11.03"
DEFAULT_CSV_EXPORT = "vcn_ip_addresses.csv"
DEFAULT_XLSX_EXPORT = "vcn_ip_addresses.xlsx"

# Parallel API calls inside the single VCN region (kept small to avoid HTTP 429)
REGION_WORKERS = 4

EXPORT_COLUMNS = [
    ("region", "Region"),
    ("vcn_name", "VCN"),
    ("network_type", "Network type"),
    ("subnet_name", "Subnet / VLAN"),
    ("subnet_cidr", "CIDR"),
    ("vlan_tag", "VLAN tag"),
    ("ip_version", "IP version"),
    ("ip_address", "IP address"),
    ("ip_type", "Type"),
    ("hostname", "Hostname"),
    ("public_ip", "Public IP"),
    ("public_ip_lifetime", "Public IP lifetime"),
    ("vnic_name", "VNIC"),
    ("attached_to", "Attached to"),
    ("compartment_path", "Compartment"),
    ("ip_id", "IP OCID"),
    ("vnic_id", "VNIC OCID"),
]

NETWORK_EXPORT_COLUMNS = [
    ("region", "Region"),
    ("vcn_name", "VCN"),
    ("network_type", "Network type"),
    ("name", "Subnet / VLAN"),
    ("cidr", "CIDR"),
    ("ipv6_cidrs", "IPv6 CIDRs"),
    ("vlan_tag", "VLAN tag"),
    ("access", "Access"),
    ("dns_label", "DNS label"),
    ("compartment_path", "Compartment"),
    ("total_addresses", "Addresses in CIDR"),
    ("ips_listed", "IPs listed"),
    ("id", "OCID"),
]
NETWORK_INT_COLUMNS = frozenset({"vlan_tag", "total_addresses", "ips_listed"})

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
    def __init__(self, total, label):
        self.total = total
        self.label = label
        self.done = 0
        self._lock = threading.Lock()

    def step(self, detail=""):
        with self._lock:
            self.done += 1
            pct = 100.0 * self.done / self.total if self.total else 100.0
            print(
                "{}: {:5.1f}% ({}/{})  {:<40}".format(
                    self.label, pct, self.done, self.total, _truncate(detail, 40)
                ),
                end="\r",
                flush=True,
            )

    def finish(self):
        print("")


def mark_region_throttled(region):
    with _region_throttle_lock:
        _region_throttle_active[region] = True


def retry_strategy_for_region(region):
    with _region_throttle_lock:
        if _region_throttle_active.get(region):
            return THROTTLE_RETRY_STRATEGY
    return oci.retry.DEFAULT_RETRY_STRATEGY


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


def get_call(region, func, *args, **kwargs):
    def _call(strategy):
        return func(*args, retry_strategy=strategy, **kwargs).data

    return call_with_manual_429_retry(_call, region)


def network_client(config, signer, region):
    regional_config = dict(config)
    regional_config["region"] = region
    return oci.core.VirtualNetworkClient(regional_config, signer=signer)


def compute_client(config, signer, region):
    regional_config = dict(config)
    regional_config["region"] = region
    return oci.core.ComputeClient(regional_config, signer=signer)


##########################################################################
# VCN lookup
##########################################################################
def _is_vcn_ocid(value):
    return value.lower().startswith("ocid1.vcn.")


def _regions_for_ocid(ocid, regions):
    """Try the region named in the OCID first (when it is a full region name)."""
    parts = ocid.split(".")
    hint = parts[3].lower() if len(parts) > 3 else ""
    first = [r for r in regions if r.lower() == hint]
    return first + [r for r in regions if r not in first]


def find_vcn_by_ocid(config, signer, ocid, regions):
    for region in _regions_for_ocid(ocid, regions):
        client = network_client(config, signer, region)
        try:
            vcn = get_call(region, client.get_vcn, ocid)
        except oci.exceptions.ServiceError as e:
            if e.status in (400, 401, 403, 404):
                continue
            raise
        return [(region, vcn)]
    return []


def _search_region_for_vcn_name(config, signer, region, compartment_ids, name):
    """Find VCNs by display name with Resource Search (one query per region)."""
    regional_config = dict(config)
    regional_config["region"] = region
    search_client = oci.resource_search.ResourceSearchClient(regional_config, signer=signer)
    query = "query vcn resources where displayName = '{}'".format(name.replace("'", "\\'"))
    details = oci.resource_search.models.StructuredSearchDetails(
        type="Structured", query=query, matching_context_type="NONE"
    )
    try:
        results = paginate(region, search_client.search_resources, search_details=details)
    except oci.exceptions.ServiceError as e:
        if e.status in (403, 404):
            return []
        raise

    client = network_client(config, signer, region)
    matches = []
    for item in results:
        if compartment_ids is not None and item.compartment_id not in compartment_ids:
            continue
        if item.lifecycle_state in ("TERMINATED", "TERMINATING"):
            continue
        try:
            vcn = get_call(region, client.get_vcn, item.identifier)
        except oci.exceptions.ServiceError as e:
            if e.status in (403, 404):
                continue
            raise
        matches.append((region, vcn))
    return matches


def find_vcns_by_name(config, signer, name, regions, compartments=None):
    """compartments: optional list to limit matches to (the -c subtree); None = whole tenancy."""
    compartment_ids = None
    if compartments is not None:
        compartment_ids = {c.details.id for c in compartments}
    matches = []
    with ThreadPoolExecutor(max_workers=max(1, len(regions))) as executor:
        futures = [
            executor.submit(
                _search_region_for_vcn_name, config, signer, region, compartment_ids, name
            )
            for region in regions
        ]
        for future in as_completed(futures):
            matches.extend(future.result())
    return sorted(matches, key=lambda m: (m[0], m[1].id))


##########################################################################
# Collect VCN resources
##########################################################################
def _scan_compartment_for_vcn(region, client, compartment, vcn_id):
    """Return (subnets, vlans, nat_gateways, errors) of the VCN in one compartment."""
    errors = []
    subnets = []
    vlans = []
    nat_gateways = []
    try:
        subnets = paginate(
            region, client.list_subnets, compartment_id=compartment.details.id, vcn_id=vcn_id
        )
    except oci.exceptions.ServiceError as e:
        if e.status not in (403, 404):
            errors.append((compartment.fullpath, "list subnets: {}".format(e.message)))
    try:
        vlans = paginate(
            region, client.list_vlans, compartment_id=compartment.details.id, vcn_id=vcn_id
        )
    except oci.exceptions.ServiceError as e:
        if e.status not in (403, 404):
            errors.append((compartment.fullpath, "list VLANs: {}".format(e.message)))
    try:
        nat_gateways = paginate(
            region, client.list_nat_gateways, compartment_id=compartment.details.id, vcn_id=vcn_id
        )
    except oci.exceptions.ServiceError as e:
        if e.status not in (403, 404):
            errors.append((compartment.fullpath, "list NAT gateways: {}".format(e.message)))
    subnets = [s for s in subnets if s.lifecycle_state not in ("TERMINATED", "TERMINATING")]
    vlans = [v for v in vlans if v.lifecycle_state not in ("TERMINATED", "TERMINATING")]
    nat_gateways = [n for n in nat_gateways if n.lifecycle_state not in ("TERMINATED", "TERMINATING")]
    return subnets, vlans, nat_gateways, errors


def collect_subnets_and_gateways(region, client, compartments, vcn_id):
    subnets = []
    vlans = []
    nat_gateways = []
    errors = []
    progress = ScanProgress(len(compartments), "Compartments")
    with ThreadPoolExecutor(max_workers=REGION_WORKERS) as executor:
        futures = {
            executor.submit(_scan_compartment_for_vcn, region, client, comp, vcn_id): comp
            for comp in compartments
        }
        for future in as_completed(futures):
            comp = futures[future]
            comp_subnets, comp_vlans, comp_nats, comp_errors = future.result()
            subnets.extend(comp_subnets)
            vlans.extend(comp_vlans)
            nat_gateways.extend(comp_nats)
            errors.extend(comp_errors)
            progress.step(comp.fullpath)
    progress.finish()
    return subnets, vlans, nat_gateways, errors


def _is_vlan(network):
    return isinstance(network, oci.core.models.Vlan)


def _list_subnet_ips(region, client, subnet):
    """List IPs of a subnet or a VLAN (VLANs have private IPv4 only)."""
    errors = []
    private_ips = []
    ipv6s = []
    label = subnet.display_name or subnet.id
    if _is_vlan(subnet):
        try:
            private_ips = paginate(region, client.list_private_ips, vlan_id=subnet.id)
        except oci.exceptions.ServiceError as e:
            if e.status not in (403, 404):
                errors.append((label, "list VLAN private IPs: {}".format(e.message)))
        return subnet, private_ips, ipv6s, errors
    try:
        private_ips = paginate(region, client.list_private_ips, subnet_id=subnet.id)
    except oci.exceptions.ServiceError as e:
        if e.status not in (403, 404):
            errors.append((label, "list private IPs: {}".format(e.message)))
    try:
        ipv6s = paginate(region, client.list_ipv6s, subnet_id=subnet.id)
    except oci.exceptions.ServiceError as e:
        if e.status not in (403, 404):
            errors.append((label, "list IPv6 addresses: {}".format(e.message)))
    return subnet, private_ips, ipv6s, errors


def _public_ip_for_private_ip(region, client, private_ip_id):
    details = oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=private_ip_id)
    try:
        return get_call(region, client.get_public_ip_by_private_ip_id, details)
    except oci.exceptions.ServiceError as e:
        if e.status in (403, 404):
            return None
        raise


def _vnic_details(region, net_client, comp_client, vnic_id):
    """Return (vnic_name, attached_to) for a VNIC."""
    try:
        vnic = get_call(region, net_client.get_vnic, vnic_id)
    except oci.exceptions.ServiceError as e:
        if e.status in (403, 404):
            return "-", "(not visible / service managed)"
        raise

    vnic_name = vnic.display_name or "-"
    attached_to = "-"
    try:
        attachments = paginate(
            region,
            comp_client.list_vnic_attachments,
            compartment_id=vnic.compartment_id,
            vnic_id=vnic_id,
        )
    except oci.exceptions.ServiceError as e:
        if e.status in (403, 404):
            return vnic_name, attached_to
        raise

    attachments = [a for a in attachments if a.lifecycle_state in ("ATTACHED", "ATTACHING")]
    if not attachments:
        return vnic_name, attached_to

    instance_id = attachments[0].instance_id
    try:
        instance = get_call(region, comp_client.get_instance, instance_id)
        attached_to = "instance: {}".format(instance.display_name or instance_id)
    except oci.exceptions.ServiceError as e:
        if e.status not in (403, 404):
            raise
        attached_to = "instance: {}".format(instance_id)
    return vnic_name, attached_to


def collect_ip_rows(config, signer, region, vcn, compartments, errors):
    net_client = network_client(config, signer, region)
    comp_client = compute_client(config, signer, region)
    vcn_name = vcn.display_name or vcn.id

    print("Searching subnets, VLANs and gateways of the VCN in {} compartments...".format(len(compartments)))
    subnets, vlans, nat_gateways, scan_errors = collect_subnets_and_gateways(
        region, net_client, compartments, vcn.id
    )
    errors.extend(scan_errors)
    networks = list(subnets) + list(vlans)

    print("Listing IP addresses in {} subnets and {} VLANs...".format(len(subnets), len(vlans)))
    subnet_ips = []
    progress = ScanProgress(len(networks), "Subnets/VLANs")
    with ThreadPoolExecutor(max_workers=REGION_WORKERS) as executor:
        futures = [executor.submit(_list_subnet_ips, region, net_client, s) for s in networks]
        for future in as_completed(futures):
            subnet, private_ips, ipv6s, ip_errors = future.result()
            subnet_ips.append((subnet, private_ips, ipv6s))
            errors.extend(ip_errors)
            progress.step(subnet.display_name or subnet.id)
    progress.finish()

    all_private_ips = [ip for _, pips, _ in subnet_ips for ip in pips]
    vnic_ids = sorted(
        {ip.vnic_id for _, pips, v6s in subnet_ips for ip in list(pips) + list(v6s) if ip.vnic_id}
    )

    print("Looking up public IPs for {} private IPs and details of {} VNICs...".format(
        len(all_private_ips), len(vnic_ids)
    ))
    public_ips = {}
    vnic_info = {}
    progress = ScanProgress(len(all_private_ips) + len(vnic_ids), "Details")
    with ThreadPoolExecutor(max_workers=REGION_WORKERS) as executor:
        pub_futures = {
            executor.submit(_public_ip_for_private_ip, region, net_client, ip.id): ip.id
            for ip in all_private_ips
        }
        vnic_futures = {
            executor.submit(_vnic_details, region, net_client, comp_client, vnic_id): vnic_id
            for vnic_id in vnic_ids
        }
        for future in as_completed(list(pub_futures) + list(vnic_futures)):
            try:
                result = future.result()
            except oci.exceptions.ServiceError as e:
                key = pub_futures.get(future) or vnic_futures.get(future)
                errors.append((key, e.message))
                progress.step()
                continue
            if future in pub_futures:
                public_ips[pub_futures[future]] = result
            else:
                vnic_info[vnic_futures[future]] = result
            progress.step()
    progress.finish()

    rows = []
    for subnet, private_ips, ipv6s in subnet_ips:
        base = {
            "region": region,
            "vcn_name": vcn_name,
            "subnet_name": subnet.display_name or subnet.id,
            "subnet_cidr": subnet.cidr_block or "-",
            "subnet_id": subnet.id,
            "network_type": "VLAN" if _is_vlan(subnet) else "Subnet",
            "vlan_tag": subnet.vlan_tag if _is_vlan(subnet) else "-",
        }
        gateway_ip = _gateway_ip(subnet.cidr_block)
        if gateway_ip:
            rows.append(dict(
                base,
                ip_version="IPv4",
                ip_address=gateway_ip,
                ip_type="GATEWAY/DNS",
                hostname="-",
                public_ip="-",
                public_ip_lifetime="-",
                vnic_name="-",
                attached_to="OCI {} gateway / DNS".format("VLAN" if _is_vlan(subnet) else "subnet"),
                compartment_path=_path(compartments, subnet.compartment_id),
                ip_id="-",
                vnic_id="-",
            ))
        for ip in private_ips:
            pub = public_ips.get(ip.id)
            vnic_name, attached_to = vnic_info.get(ip.vnic_id, ("-", "-"))
            if _is_vlan(subnet):
                attached_to = _vlan_attached_to(ip, pub)
            rows.append(dict(
                base,
                ip_version="IPv4",
                ip_address=ip.ip_address,
                ip_type="VLAN" if _is_vlan(subnet) else _private_ip_type(ip),
                hostname=ip.hostname_label or "-",
                public_ip=pub.ip_address if pub else "-",
                public_ip_lifetime=pub.lifetime if pub else "-",
                vnic_name=vnic_name,
                attached_to=attached_to,
                compartment_path=_path(compartments, ip.compartment_id),
                ip_id=ip.id,
                vnic_id=ip.vnic_id or "-",
            ))
        for ip in ipv6s:
            if getattr(ip, "lifecycle_state", None) in ("TERMINATED", "TERMINATING"):
                continue
            vnic_name, attached_to = vnic_info.get(ip.vnic_id, ("-", "-"))
            rows.append(dict(
                base,
                ip_version="IPv6",
                ip_address=ip.ip_address,
                ip_type="IPv6",
                hostname="-",
                public_ip="-",
                public_ip_lifetime="-",
                vnic_name=vnic_name,
                attached_to=attached_to,
                compartment_path=_path(compartments, ip.compartment_id),
                ip_id=ip.id,
                vnic_id=ip.vnic_id or "-",
            ))

    for nat in nat_gateways:
        if not nat.nat_ip:
            continue
        nat_lifetime = _nat_public_ip_lifetime(region, net_client, nat)
        rows.append({
            "region": region,
            "vcn_name": vcn_name,
            "network_type": "-",
            "subnet_name": "-",
            "subnet_cidr": "-",
            "vlan_tag": "-",
            "ip_version": "IPv4",
            "ip_address": nat.nat_ip,
            "ip_type": "NAT gateway",
            "hostname": "-",
            "public_ip": nat.nat_ip,
            "public_ip_lifetime": nat_lifetime,
            "vnic_name": "-",
            "attached_to": "NAT gateway: {}".format(nat.display_name or nat.id),
            "compartment_path": _path(compartments, nat.compartment_id),
            "ip_id": getattr(nat, "public_ip_id", None) or "-",
            "vnic_id": "-",
        })

    return rows, networks


def _vlan_attached_to(private_ip, public_ip):
    """VLAN IPs have no VNIC: show the name of the external access (public) IP instead."""
    if public_ip is not None:
        return "external access: {}".format(
            public_ip.display_name or private_ip.display_name or public_ip.ip_address
        )
    if private_ip.display_name:
        return private_ip.display_name
    return "-"


def _nat_public_ip_lifetime(region, client, nat):
    public_ip_id = getattr(nat, "public_ip_id", None)
    if not public_ip_id:
        return "-"
    try:
        public_ip = get_call(region, client.get_public_ip, public_ip_id)
    except oci.exceptions.ServiceError as e:
        if e.status in (403, 404):
            return "-"
        raise
    return public_ip.lifetime or "-"


def _gateway_ip(cidr_block):
    """First usable IP of a CIDR range (OCI default gateway / DNS address)."""
    if not cidr_block:
        return None
    try:
        network = ipaddress.ip_network(cidr_block, strict=False)
    except ValueError:
        return None
    if network.num_addresses < 2:
        return None
    return str(network.network_address + 1)


def _private_ip_type(ip):
    if not ip.vnic_id:
        return "Unassigned"
    if ip.is_primary:
        return "Primary"
    return "Secondary"


def _path(compartments, compartment_id):
    return GetCompartmentFullPath(compartments, compartment_id) or (compartment_id or "-")


##########################################################################
# Output
##########################################################################
def _ip_sort_key(value):
    try:
        addr = ipaddress.ip_address(value)
        return (addr.version, int(addr))
    except ValueError:
        return (9, 0)


def _sorted_rows(rows):
    return sorted(
        rows,
        key=lambda r: (
            r["subnet_name"] == "-",
            _ip_sort_key(r["subnet_cidr"].split("/")[0]) if r["subnet_cidr"] != "-" else (9, 0),
            r["subnet_name"],
            _ip_sort_key(r["ip_address"]),
        ),
    )


def _export_records(rows):
    return [{key: row.get(key, "") for key, _ in EXPORT_COLUMNS} for row in rows]


def _write_csv_file(path, export_columns, records):
    fieldnames = [key for key, _ in export_columns]
    headers = {key: title for key, title in export_columns}
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writerow(headers)
        writer.writerows(records)


def csv_export_paths(path):
    """report.csv -> (report_segments.csv, report_ips.csv)"""
    base, ext = os.path.splitext(path)
    ext = ext or ".csv"
    return base + "_segments" + ext, base + "_ips" + ext


def write_csv_export(path, rows, network_rows):
    """Write two CSV files: <name>_segments.csv (subnets / VLANs) and <name>_ips.csv (IP listing)."""
    segments_path, ips_path = csv_export_paths(path)
    _write_csv_file(segments_path, NETWORK_EXPORT_COLUMNS, network_rows)
    _write_csv_file(ips_path, EXPORT_COLUMNS, _export_records(rows))
    return segments_path, ips_path


def write_excel_export(path, rows, network_rows):
    write_excel_export_sheets(path, [
        {
            "title": "Subnets and VLANs",
            "export_columns": NETWORK_EXPORT_COLUMNS,
            "rows": network_rows,
            "int_columns": NETWORK_INT_COLUMNS,
        },
        {
            "title": "IP addresses",
            "export_columns": EXPORT_COLUMNS,
            "rows": _export_records(rows),
            "int_columns": frozenset({"vlan_tag"}),
        },
    ])


def build_network_rows(region, vcn, networks, rows, compartments):
    """One row per subnet / VLAN of the VCN, sorted by CIDR."""
    counts = {}
    for row in rows:
        key = row.get("subnet_id")
        counts[key] = counts.get(key, 0) + 1

    result = []
    for network in sorted(networks, key=lambda n: _ip_sort_key((n.cidr_block or "").split("/")[0])):
        vlan = _is_vlan(network)
        if vlan:
            access = "VLAN {}".format(network.vlan_tag)
        else:
            access = "private" if network.prohibit_public_ip_on_vnic else "public"
        try:
            total = ipaddress.ip_network(network.cidr_block, strict=False).num_addresses
        except (TypeError, ValueError):
            total = "-"
        ipv6 = getattr(network, "ipv6_cidr_blocks", None) or (
            [network.ipv6_cidr_block] if getattr(network, "ipv6_cidr_block", None) else []
        )
        result.append({
            "region": region,
            "vcn_name": vcn.display_name or vcn.id,
            "network_type": "VLAN" if vlan else "Subnet",
            "name": network.display_name or network.id,
            "cidr": network.cidr_block or "-",
            "ipv6_cidrs": ", ".join(ipv6) if ipv6 else "-",
            "vlan_tag": network.vlan_tag if vlan else "-",
            "access": access,
            "dns_label": getattr(network, "dns_label", None) or "-",
            "compartment_path": _path(compartments, network.compartment_id),
            "total_addresses": total,
            "ips_listed": counts.get(network.id, 0),
            "id": network.id,
        })
    return result


def print_vcn_matches(matches, compartments):
    header = "{:<18} {:<28} {:<36} {}".format("Region", "VCN", "Compartment", "OCID")
    print(header)
    print("-" * len(header))
    for region, vcn in matches:
        print("{:<18} {:<28} {:<36} {}".format(
            region,
            _truncate(vcn.display_name or "-", 28),
            _truncate(_path(compartments, vcn.compartment_id), 36),
            vcn.id,
        ))


def print_subnet_summary(network_rows):
    header = "{:<30} {:<20} {:<10} {:>8}".format("Subnet / VLAN", "CIDR", "Access", "IPs used")
    print(header)
    print("-" * len(header))
    for net in network_rows:
        print("{:<30} {:<20} {:<10} {:>8}".format(
            _truncate(net["name"], 30), net["cidr"], net["access"], net["ips_listed"]
        ))


def print_ip_table(rows):
    fmt = "{:<24} {:<26} {:<22} {:<16} {:<12} {:<20} {:<40}"
    header = fmt.format(
        "Subnet / VLAN", "IP address", "Type", "Public IP", "PIP lifetime", "Hostname", "Attached to"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(fmt.format(
            _truncate(row["subnet_name"], 24),
            _truncate(row["ip_address"], 26),
            _truncate(row["ip_type"], 22),
            _truncate(row["public_ip"], 16),
            _truncate(row["public_ip_lifetime"], 12),
            _truncate(row["hostname"], 20),
            _truncate(row["attached_to"], 40),
        ))


##########################################################################
# Main
##########################################################################
def parse_cmd():
    cmd = input_command_line(export_options=True, vcn_option=True)
    resolve_export_paths(cmd, DEFAULT_CSV_EXPORT, DEFAULT_XLSX_EXPORT)
    return cmd


def main():
    check_oci_version(MIN_OCI_VERSION)
    cmd = parse_cmd()

    if not cmd.vcn:
        print("Please specify the VCN with -vcn <OCID or name>  (see -h for help)")
        sys.exit(1)

    writer = None
    if cmd.log_file:
        writer = MyWriter(sys.stdout, cmd.log_file)
        sys.stdout = writer

    try:
        print("Script: list_vcn_ip_addresses.py  Version: {}".format(SCRIPT_VERSION))
        print("Listing all IP addresses used in a VCN...")
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

        compartments = GetFullCompartments(config, signer)
        selected_compartment_id = None
        if cmd.compartment:
            selected = FindCompartment(compartments, cmd.compartment)
            if selected is None:
                print("Compartment '{}' not found, aborting".format(cmd.compartment))
                sys.exit(1)
            selected_compartment_id = selected.details.id
            print("Compartment filter (VCN name search): {} ({})".format(
                selected.fullpath, selected_compartment_id
            ))

        if _is_vcn_ocid(cmd.vcn):
            print("Looking up VCN by OCID in {} region(s)...".format(len(regions)))
            matches = find_vcn_by_ocid(config, signer, cmd.vcn, regions)
        else:
            search_compartments = None
            if selected_compartment_id:
                search_compartments = compartments_to_scan(compartments, selected_compartment_id)
            print("Searching VCN named '{}' with Resource Search in {} region(s)...".format(
                cmd.vcn, len(regions)
            ))
            matches = find_vcns_by_name(config, signer, cmd.vcn, regions, search_compartments)
        print("")

        if not matches:
            print("VCN '{}' not found, aborting".format(cmd.vcn))
            sys.exit(1)
        if len(matches) > 1:
            print("VCN name '{}' matches {} VCNs. Re-run with -vcn <OCID>, or narrow with -c / -rg:".format(
                cmd.vcn, len(matches)
            ))
            print("")
            print_vcn_matches(matches, compartments)
            sys.exit(1)

        region, vcn = matches[0]
        cidrs = getattr(vcn, "cidr_blocks", None) or [vcn.cidr_block]
        ipv6_cidrs = getattr(vcn, "ipv6_cidr_blocks", None) or []
        print("VCN        : {} ({})".format(vcn.display_name, vcn.id))
        print("Region     : {}".format(region))
        print("Compartment: {}".format(_path(compartments, vcn.compartment_id)))
        print("CIDR blocks: {}".format(", ".join(c for c in cidrs + ipv6_cidrs if c)))
        print("")

        errors = []
        rows, networks = collect_ip_rows(config, signer, region, vcn, compartments, errors)
        rows = _sorted_rows(rows)

        print("")
        network_rows = build_network_rows(region, vcn, networks, rows, compartments)
        print_subnet_summary(network_rows)
        print("")
        print_ip_table(rows)
        print("")

        ipv4 = [
            r for r in rows
            if r["ip_version"] == "IPv4" and r["ip_type"] not in ("NAT gateway", "GATEWAY/DNS")
        ]
        ipv6 = [r for r in rows if r["ip_version"] == "IPv6"]
        public = {r["public_ip"] for r in rows if r["public_ip"] != "-"}
        subnet_count = len([n for n in networks if not _is_vlan(n)])
        print("Subnets: {}  VLANs: {}  Private IPv4: {}  IPv6: {}  Public IPv4: {}".format(
            subnet_count, len(networks) - subnet_count, len(ipv4), len(ipv6), len(public)
        ))

        if cmd.csv_export:
            for written in write_csv_export(cmd.csv_export, rows, network_rows):
                print("CSV export written: {}".format(os.path.abspath(written)))
        if cmd.excel_export:
            write_excel_export(cmd.excel_export, rows, network_rows)
            print("Excel export written: {}".format(os.path.abspath(cmd.excel_export)))

        if errors:
            print("Non-fatal errors (first 10):")
            for where, message in errors[:10]:
                print("  {}  {}".format(where, message))
            if len(errors) > 10:
                print("  ... and {} more".format(len(errors) - 10))

    finally:
        if writer is not None:
            sys.stdout = writer.stdout
            writer.close()


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
