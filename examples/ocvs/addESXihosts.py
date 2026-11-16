"""
Interactive workflow to add ESXi hosts to an Oracle Cloud VMware Solution (OCVS) cluster.

The script discovers ACTIVE SDDCs via Resource Search in the configured region (unless -rg is set),
then guides you through
SDDC, cluster, host shape, VMware/ESXi software version, optional VCF BYOL allocation,
optional bare-metal capacity reservation, and host count, shows a summary, and on
confirmation calls CreateEsxiHost for each node. Reservations are discovered in the
selected region and compartment scope (-c), and must match the cluster's availability
domain and selected shape/OCPU count. Multi-AD SDDCs use on-demand capacity only.
Reservations with available capacity outside a specific fault domain require an
SDK supporting initial_fault_domain_host_distribution and explicit confirmation
to use uneven host distribution, which can reduce availability.

IAM: inspect/list SDDCs, clusters, supported shapes and software versions, BYOL allocations;
      manage ESXi hosts (create) in the target compartment.
      For reserved capacity: read compute-capacity-reservations in the reservation
      compartments and permission to use the selected compute capacity reservation.

Usage (from repository root):
    python examples/ocvs/addESXihosts.py
    python examples/ocvs/addESXihosts.py -cp MYPROFILE -log add_esxi.txt
    python examples/ocvs/addESXihosts.py -rg eu-frankfurt-1
    python examples/ocvs/addESXihosts.py -c /root/prod
"""

import os
import sys
import uuid

import oci

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

MIN_OCI_VERSION = "2.185.2"
SCRIPT_VERSION = "26.11.16"

CREATE_ESXI_HOST = oci.ocvp.models.SupportedHostShapeSummary.SUPPORTED_OPERATIONS_CREATE_ESXI_HOST
SDDC_SEARCH_QUERY = "query vmwaresddc resources"


class SddcRef(object):
    """SDDC discovered during scan."""

    __slots__ = ("region", "compartment_id", "compartment_path", "summary")

    def __init__(self, region, compartment_id, compartment_path, summary):
        self.region = region
        self.compartment_id = compartment_id
        self.compartment_path = compartment_path
        self.summary = summary


def _ocvp_clients(config, signer, region):
    regional_config = dict(config)
    regional_config["region"] = region
    return {
        "sddc": oci.ocvp.SddcClient(regional_config, signer=signer),
        "cluster": oci.ocvp.ClusterClient(regional_config, signer=signer),
        "esxi": oci.ocvp.EsxiHostClient(regional_config, signer=signer),
        "byol": oci.ocvp.ByolAllocationClient(regional_config, signer=signer),
    }


def _paginate(list_func, **kwargs):
    return oci.pagination.list_call_get_all_results(list_func, **kwargs).data


def _compartment_path_by_id(compartments):
    return {c.details.id: c.fullpath for c in compartments}


def _compartments_to_scan(all_compartments, selected_compartment_id):
    if not selected_compartment_id:
        return all_compartments

    def in_subtree(compartment_id):
        if compartment_id == selected_compartment_id:
            return True
        path_by_id = {c.details.id: c.fullpath for c in all_compartments}
        selected_path = path_by_id.get(selected_compartment_id)
        instance_path = path_by_id.get(compartment_id)
        if not selected_path or not instance_path:
            return compartment_id == selected_compartment_id
        return instance_path == selected_path or instance_path.startswith(selected_path + "/")

    return [c for c in all_compartments if in_subtree(c.details.id)]


def discover_sddcs(config, signer, regions, compartment_paths, compartment_ids_filter=None):
    """Discover ACTIVE SDDCs via Resource Search per region; enrich with get_sddc."""
    refs = []
    seen_ids = set()

    for region in regions:
        regional_config = dict(config)
        regional_config["region"] = region
        search_client = oci.resource_search.ResourceSearchClient(
            regional_config, signer=signer
        )
        search_details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured",
            query=SDDC_SEARCH_QUERY,
            matching_context_type="NONE",
        )
        try:
            hits = _paginate(
                search_client.search_resources,
                search_details=search_details,
            )
        except oci.exceptions.ServiceError as e:
            if e.status in (403, 404):
                continue
            print("  Warning: Resource Search failed in {}: {}".format(region, e.message))
            continue

        ocvp_clients = _ocvp_clients(config, signer, region)
        for item in hits:
            if item.identifier in seen_ids:
                continue
            if item.resource_type and item.resource_type.lower() != "vmwaresddc":
                continue
            if compartment_ids_filter and item.compartment_id not in compartment_ids_filter:
                continue
            if item.lifecycle_state in ("DELETED", "DELETING", "TERMINATED", "TERMINATING"):
                continue

            seen_ids.add(item.identifier)
            compartment_path = compartment_paths.get(
                item.compartment_id, item.compartment_id
            )
            try:
                summary = ocvp_clients["sddc"].get_sddc(item.identifier).data
            except oci.exceptions.ServiceError as e:
                if e.status in (403, 404):
                    continue
                print("  Warning: get_sddc failed in {} for {}: {}".format(
                    region, item.identifier, e.message,
                ))
                continue
            if summary.lifecycle_state != "ACTIVE":
                continue

            refs.append(
                SddcRef(
                    region,
                    item.compartment_id,
                    compartment_path,
                    summary,
                )
            )

    refs.sort(
        key=lambda r: (
            r.region,
            r.compartment_path,
            (r.summary.display_name or "").lower(),
        )
    )
    return refs


def prompt_choice(title, options, label_fn, allow_back=False):
    """
    Print a numbered menu and return the chosen item.
    Re-prompts on invalid input. Empty options returns None.
    """
    if not options:
        print("{}: (none found)".format(title))
        return None

    while True:
        print()
        print(title)
        print("-" * min(72, len(title) + 2))
        for i, item in enumerate(options, start=1):
            print("  {:3d}. {}".format(i, label_fn(item)))
        if allow_back:
            print("    0. Back / cancel this step")
        try:
            raw = input("Enter number: ").strip()
        except EOFError:
            print()
            return None
        if allow_back and raw in ("0", "q", "Q", "back"):
            return None
        if not raw.isdigit():
            print("  Please enter a number from the list.")
            continue
        idx = int(raw)
        if idx < 1 or idx > len(options):
            print("  Invalid selection.")
            continue
        return options[idx - 1]


def prompt_positive_int(prompt, maximum=None):
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError:
            print()
            return None
        if not raw.isdigit():
            print("  Enter a positive integer.")
            continue
        value = int(raw)
        if value < 1:
            print("  Enter at least 1.")
            continue
        if maximum is not None and value > maximum:
            print("  Maximum allowed here is {}.".format(maximum))
            continue
        return value


def confirm(prompt):
    while True:
        try:
            raw = input("{} [y/N]: ".format(prompt)).strip().lower()
        except EOFError:
            print()
            return False
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no", ""):
            return False
        print("  Enter y or n.")


def list_clusters_for_sddc(clients, sddc_id, compartment_id):
    try:
        return _paginate(
            clients["cluster"].list_clusters,
            sddc_id=sddc_id,
            compartment_id=compartment_id,
            lifecycle_state="ACTIVE",
        )
    except oci.exceptions.ServiceError as e:
        print("Failed to list clusters: {}".format(e.message))
        return []


def get_cluster(clients, cluster_id):
    return clients["cluster"].get_cluster(cluster_id).data


def list_host_shapes(client, compartment_id, initial_host_shape_name):
    try:
        shapes = _paginate(
            client.list_supported_host_shapes,
            compartment_id=compartment_id,
            initial_host_shape_name=initial_host_shape_name,
        )
    except oci.exceptions.ServiceError as e:
        print("Failed to list supported host shapes: {}".format(e.message))
        return []
    result = []
    for shape in shapes:
        ops = shape.supported_operations or []
        if CREATE_ESXI_HOST in ops:
            result.append(shape)
    return result


def list_vmware_versions(client, compartment_id, host_shape_name):
    try:
        return _paginate(
            client.list_supported_vmware_software_versions,
            compartment_id=compartment_id,
            host_shape_name=host_shape_name,
        )
    except oci.exceptions.ServiceError as e:
        print("Failed to list VMware software versions: {}".format(e.message))
        return []


def list_vcf_byol_allocations(client, compartment_id):
    try:
        items = _paginate(
            client.list_byol_allocations,
            compartment_id=compartment_id,
            software_type="VCF",
            lifecycle_state="ACTIVE",
        )
    except oci.exceptions.ServiceError as e:
        print("Failed to list BYOL allocations: {}".format(e.message))
        return []
    return [a for a in items if (a.available_units or 0) > 0]


def esxi_versions_for_shape(vmware_bundle, host_shape_name):
    candidates = []
    for esxi in vmware_bundle.esxi_software_versions or []:
        names = esxi.supported_host_shape_names or []
        if not names or host_shape_name in names:
            candidates.append(esxi)
    return candidates


def _matching_reservation_configs(reservation, host_shape, host_ocpu):
    """Return matching reservation configurations that still have free hosts."""
    matches = []
    for capacity in reservation.instance_reservation_configs or []:
        if capacity.instance_shape != host_shape:
            continue
        shape_config = capacity.instance_shape_config
        if (shape_config is not None and shape_config.ocpus is not None
                and shape_config.ocpus != host_ocpu):
            continue
        if (capacity.reserved_count or 0) > (capacity.used_count or 0):
            matches.append(capacity)
    return matches


def list_capacity_reservations(client, compartments, availability_domain, host_shape, host_ocpu):
    """Return (reservation, matching free host count) pairs within the CLI scope."""
    result = []
    for compartment in compartments:
        try:
            reservations = _paginate(
                client.list_compute_capacity_reservations,
                compartment_id=compartment.details.id,
                availability_domain=availability_domain,
                lifecycle_state="ACTIVE",
            )
        except oci.exceptions.ServiceError as e:
            print("  Warning: cannot list capacity reservations in {}: {}".format(
                compartment.fullpath, e.message,
            ))
            continue
        for summary in reservations:
            try:
                reservation = client.get_compute_capacity_reservation(summary.id).data
            except oci.exceptions.ServiceError as e:
                print("  Warning: cannot read capacity reservation {}: {}".format(
                    summary.id, e.message,
                ))
                continue
            if (reservation.lifecycle_state != "ACTIVE"
                    or reservation.availability_domain != availability_domain):
                continue
            available = sum(
                (capacity.reserved_count or 0) - (capacity.used_count or 0)
                for capacity in _matching_reservation_configs(reservation, host_shape, host_ocpu)
            )
            if available:
                result.append((reservation, available))
    result.sort(key=lambda item: ((item[0].display_name or "").lower(), item[0].id))
    return result


def pick_host_ocpu(shape, cluster):
    supported = shape.supported_ocpu_count or []
    if len(supported) == 1:
        return supported[0]
    if len(supported) > 1:
        def label(ocpu):
            return "{:g} OCPUs".format(ocpu)

        chosen = prompt_choice("Select OCPU count for shape {}".format(shape.name), supported, label)
        return chosen
    if cluster.initial_host_ocpu_count is not None:
        return cluster.initial_host_ocpu_count
    return shape.default_ocpu_count


def print_summary(selection):
    print()
    print("=" * 72)
    print("SUMMARY — review before provisioning")
    print("=" * 72)
    print("  Region:              {}".format(selection["region"]))
    print("  Compartment:         {}".format(selection["compartment_path"]))
    print("  SDDC:                {} ({})".format(
        selection["sddc_name"], selection["sddc_id"]
    ))
    print("  Cluster:             {} ({})".format(
        selection["cluster_name"], selection["cluster_id"]
    ))
    print("  Host shape:          {}".format(selection["host_shape"]))
    print("  OCPUs per host:      {}".format(selection["host_ocpu_count"]))
    if selection.get("capacity_reservation_id"):
        print("  Capacity reservation: {} ({})".format(
            selection["capacity_reservation_name"], selection["capacity_reservation_id"],
        ))
        print("  Reserved hosts free: {} (at selection time)".format(
            selection["capacity_available_hosts"],
        ))
        if selection.get("initial_fault_domain_host_distribution"):
            print("  Host distribution:   Unevenly distributed (fault domain balancing disabled)")
            print("  Warning: uneven host distribution can reduce availability.")
    else:
        print("  Capacity type:       On-demand")
    print("  VMware bundle:       {} — {}".format(
        selection["vmware_version"], selection["vmware_description"]
    ))
    print("  ESXi version:        {}".format(selection["esxi_version"]))
    if selection["byol_allocation_id"]:
        print("  VCF BYOL allocation: {} ({}, {} units available)".format(
            selection["byol_name"],
            selection["byol_allocation_id"],
            selection["byol_available_units"],
        ))
    else:
        print("  VCF BYOL allocation: (none)")
    print("  Host count:          {}".format(selection["host_count"]))
    print("  Billing commitment:  {} (from cluster)".format(selection["commitment"]))
    print("=" * 72)


def create_esxi_hosts(clients, selection):
    work_request_ids = []
    errors = []

    for index in range(selection["host_count"]):
        details = oci.ocvp.models.CreateEsxiHostDetails(
            cluster_id=selection["cluster_id"],
            host_shape_name=selection["host_shape"],
            esxi_software_version=selection["esxi_version"],
            host_ocpu_count=selection["host_ocpu_count"],
            current_commitment=selection["commitment"],
        )
        if selection["byol_allocation_id"]:
            details.vcf_byol_allocation_id = selection["byol_allocation_id"]
        if selection["compute_availability_domain"]:
            details.compute_availability_domain = selection["compute_availability_domain"]
        if selection.get("capacity_reservation_id"):
            details.capacity_reservation_id = selection["capacity_reservation_id"]
        if selection.get("initial_fault_domain_host_distribution"):
            if "initial_fault_domain_host_distribution" not in details.swagger_types:
                raise RuntimeError("Upgrade the OCI SDK to support uneven host distribution.")
            details.initial_fault_domain_host_distribution = selection["initial_fault_domain_host_distribution"]

        try:
            response = clients["esxi"].create_esxi_host(
                create_esxi_host_details=details,
                opc_retry_token=str(uuid.uuid4()),
            )
        except oci.exceptions.ServiceError as e:
            errors.append("Host {}: {}".format(index + 1, e.message))
            continue

        wr_id = None
        if response.headers is not None:
            wr_id = response.headers.get("opc-work-request-id")
        work_request_ids.append(wr_id or "(no work-request id in response)")
        print("  Submitted ESXi host {} of {} — work request: {}".format(
            index + 1, selection["host_count"], work_request_ids[-1],
        ))

    return work_request_ids, errors


def main():
    check_oci_version(MIN_OCI_VERSION)
    cmd = input_command_line()

    writer = None
    if cmd.log_file:
        writer = MyWriter(sys.stdout, cmd.log_file)
        sys.stdout = writer

    try:
        print("Script version: {}".format(SCRIPT_VERSION))
        print("Oracle Cloud VMware Solution — add ESXi hosts (interactive)")
        print()

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

        all_compartments = GetFullCompartments(config, signer)
        selected_compartment_id = None
        if cmd.compartment:
            match = FindCompartment(all_compartments, cmd.compartment)
            if match is None:
                print("Compartment not found: {}".format(cmd.compartment))
                return 1
            selected_compartment_id = match.details.id
            print("Compartment filter: {}".format(match.fullpath))

        compartments = _compartments_to_scan(all_compartments, selected_compartment_id)
        compartment_paths = _compartment_path_by_id(all_compartments)
        compartment_ids_filter = None
        if selected_compartment_id:
            compartment_ids_filter = {c.details.id for c in compartments}

        if cmd.region:
            if cmd.region not in SubscribedRegions(config, signer):
                print("Region {} is not subscribed.".format(cmd.region))
                return 1
            regions = [cmd.region]
        else:
            configured_region = config.get("region")
            if not configured_region:
                print("No region configured; specify a region in the OCI config or with -rg.")
                return 1
            regions = [configured_region]

        print("Searching for SDDCs (Resource Search) in {}...".format(regions[0]))
        sddc_refs = discover_sddcs(
            config,
            signer,
            regions,
            compartment_paths,
            compartment_ids_filter=compartment_ids_filter,
        )
        if not sddc_refs:
            print("No ACTIVE SDDCs found in scope.")
            return 0

        def sddc_label(ref):
            name = ref.summary.display_name or ref.summary.id
            return "{} | {} | {} | clusters={}".format(
                name,
                ref.region,
                ref.compartment_path,
                ref.summary.clusters_count if ref.summary.clusters_count is not None else "?",
            )

        sddc_ref = prompt_choice("Select SDDC", sddc_refs, sddc_label)
        if sddc_ref is None:
            print("Cancelled.")
            return 0

        clients = _ocvp_clients(config, signer, sddc_ref.region)
        sddc_compartment_id = (
            sddc_ref.summary.compartment_id or sddc_ref.compartment_id
        )
        clusters = list_clusters_for_sddc(
            clients, sddc_ref.summary.id, sddc_compartment_id
        )
        if not clusters:
            print("No ACTIVE clusters in this SDDC.")
            return 0

        def cluster_label(c):
            software = getattr(c, "esxi_software_version", None) or c.vmware_software_version or "-"
            hosts = c.esxi_hosts_count if c.esxi_hosts_count is not None else "?"
            return "{} | VMware {} | shape {} | hosts={}".format(
                c.display_name or c.id,
                software,
                c.initial_host_shape_name or "-",
                hosts,
            )

        cluster_summary = prompt_choice("Select cluster", clusters, cluster_label)
        if cluster_summary is None:
            print("Cancelled.")
            return 0

        cluster = get_cluster(clients, cluster_summary.id)
        initial_shape = cluster.initial_host_shape_name
        if not initial_shape:
            print("Cluster has no initial host shape; cannot determine compatible add-host shapes.")
            return 1

        shapes = list_host_shapes(clients["sddc"], sddc_ref.compartment_id, initial_shape)
        if not shapes:
            print("No host shapes support CREATE_ESXI_HOST for this cluster.")
            return 0

        def shape_label(s):
            ocpus = s.default_ocpu_count
            extra = ""
            if s.supported_ocpu_count:
                extra = " (OCPUs: {})".format(
                    ", ".join(str(x) for x in s.supported_ocpu_count)
                )
            elif ocpus is not None:
                extra = " (default {} OCPUs)".format(ocpus)
            return "{}{}".format(s.name, extra)

        shape = prompt_choice("Select host shape", shapes, shape_label)
        if shape is None:
            print("Cancelled.")
            return 0

        vmware_bundles = list_vmware_versions(
            clients["sddc"], sddc_ref.compartment_id, shape.name
        )
        if not vmware_bundles:
            print("No supported VMware software versions for shape {}.".format(shape.name))
            return 0

        def vmware_label(v):
            return "{} — {}".format(v.version, (v.description or "")[:60])

        vmware_bundle = prompt_choice(
            "Select VMware software version (bundle)", vmware_bundles, vmware_label
        )
        if vmware_bundle is None:
            print("Cancelled.")
            return 0

        esxi_candidates = esxi_versions_for_shape(vmware_bundle, shape.name)
        if not esxi_candidates:
            if cluster.esxi_software_version:
                esxi_version = cluster.esxi_software_version
                print("Using cluster ESXi version: {}".format(esxi_version))
            else:
                print("No ESXi versions in bundle for this shape.")
                return 0
        elif len(esxi_candidates) == 1:
            esxi_version = esxi_candidates[0].version
            print("ESXi version: {} — {}".format(
                esxi_version, esxi_candidates[0].description or ""
            ))
        else:
            esxi_version = prompt_choice(
                "Select ESXi software version",
                esxi_candidates,
                lambda e: "{} — {}".format(e.version, e.description or ""),
            )
            if esxi_version is None:
                print("Cancelled.")
                return 0
            esxi_version = esxi_version.version

        byol_allocations = list_vcf_byol_allocations(clients["byol"], sddc_ref.compartment_id)

        class _NoByol(object):
            id = None
            display_name = "(Do not use VCF BYOL allocation)"
            available_units = None

        byol_options = [_NoByol()] + list(byol_allocations)

        def byol_label(a):
            if a.id is None:
                return a.display_name
            return "{} | available={} of {}".format(
                a.display_name or a.id,
                a.available_units,
                a.allocated_units,
            )

        byol_choice = prompt_choice("Select VCF BYOL allocation", byol_options, byol_label)
        if byol_choice is None:
            print("Cancelled.")
            return 0

        host_ocpu = pick_host_ocpu(shape, cluster)
        if host_ocpu is None:
            print("Cancelled.")
            return 0

        reservation = None
        capacity_available_hosts = None
        host_distribution = None
        availability_domain = cluster.compute_availability_domain
        if (shape.name.startswith("BM.") and availability_domain
                and availability_domain.lower() != "multi-ad"):
            capacity_type = prompt_choice(
                "Select capacity type", ["On-demand", "Capacity reservation"],
                lambda item: item, allow_back=True,
            )
            if capacity_type is None:
                print("Cancelled.")
                return 0
            if capacity_type == "Capacity reservation":
                if "capacity_reservation_id" not in oci.ocvp.models.CreateEsxiHostDetails().swagger_types:
                    print("Installed OCI SDK does not support ESXi capacity reservations; upgrade oci.")
                    return 1
                regional_config = dict(config)
                regional_config["region"] = sddc_ref.region
                compute_client = oci.core.ComputeClient(regional_config, signer=signer)
                print("Searching for matching capacity reservations...")
                reservations = list_capacity_reservations(
                    compute_client, compartments, availability_domain, shape.name, host_ocpu,
                )

                def reservation_label(item):
                    reserved, available = item
                    return "{} | {} | {} | available={} | {}".format(
                        reserved.display_name or reserved.id,
                        compartment_paths.get(reserved.compartment_id, reserved.compartment_id),
                        reserved.availability_domain, available, reserved.id,
                    )

                choice = prompt_choice(
                    "Select capacity reservation", reservations, reservation_label, allow_back=True,
                )
                if choice is None:
                    print("No capacity reservation selected; no hosts were created.")
                    return 0
                reservation, capacity_available_hosts = choice
                matching_configs = _matching_reservation_configs(reservation, shape.name, host_ocpu)
                if any(not capacity.fault_domain for capacity in matching_configs):
                    print("This reservation includes available capacity without a specific fault domain.")
                    print("OCI requires fault domain balancing to be disabled to use that capacity.")
                    if "initial_fault_domain_host_distribution" not in oci.ocvp.models.CreateEsxiHostDetails().swagger_types:
                        print("Installed OCI SDK does not support this host distribution option.")
                        print("Upgrade with: python3 -m pip install --upgrade oci")
                        print("Then rerun the script, or use a reservation with a specific fault domain.")
                        return 1
                    print("Warning: uneven host distribution can reduce availability.")
                    if not confirm("Use uneven host distribution for the new hosts?"):
                        print("Cancelled.")
                        return 0
                    host_distribution = "UNEVENLY_DISTRIBUTED"
        else:
            print("Using on-demand capacity (reservations require a BM shape and single-AD SDDC/cluster).")

        max_hosts = None
        if byol_choice.id and byol_choice.available_units is not None:
            max_hosts = byol_choice.available_units
        if capacity_available_hosts is not None:
            max_hosts = min(max_hosts, capacity_available_hosts) if max_hosts is not None else capacity_available_hosts

        host_count = prompt_positive_int(
            "How many ESXi hosts to add? ",
            maximum=max_hosts,
        )
        if host_count is None:
            print("Cancelled.")
            return 0

        selection = {
            "region": sddc_ref.region,
            "compartment_path": sddc_ref.compartment_path,
            "sddc_id": sddc_ref.summary.id,
            "sddc_name": sddc_ref.summary.display_name or sddc_ref.summary.id,
            "cluster_id": cluster.id,
            "cluster_name": cluster.display_name or cluster.id,
            "host_shape": shape.name,
            "host_ocpu_count": host_ocpu,
            "capacity_reservation_id": reservation.id if reservation else None,
            "capacity_reservation_name": reservation.display_name if reservation else None,
            "capacity_available_hosts": capacity_available_hosts,
            "initial_fault_domain_host_distribution": host_distribution,
            "vmware_version": vmware_bundle.version,
            "vmware_description": vmware_bundle.description or "",
            "esxi_version": esxi_version,
            "byol_allocation_id": byol_choice.id,
            "byol_name": byol_choice.display_name if byol_choice.id else None,
            "byol_available_units": byol_choice.available_units,
            "host_count": host_count,
            "commitment": cluster.initial_commitment,
            "compute_availability_domain": cluster.compute_availability_domain,
        }

        print_summary(selection)
        if not confirm("Proceed and create ESXi host(s)?"):
            print("Aborted; no hosts were created.")
            return 0

        print()
        print("Creating {} ESXi host(s)...".format(host_count))
        work_requests, errors = create_esxi_hosts(clients, selection)

        print()
        print("Done. Submitted {} create request(s).".format(len(work_requests)))
        for wr in work_requests:
            print("  {}".format(wr))
        if errors:
            print("Errors:")
            for err in errors:
                print("  {}".format(err))
            return 1
        return 0

    finally:
        if writer is not None:
            sys.stdout = writer.terminal
            writer.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
