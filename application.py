"""
OCI Python Script Template

Copy this file to start a new OCI (Oracle Cloud Infrastructure) script.
It handles authentication, logging, region and compartment discovery so
the script-specific logic can be added in the "Your code starts here" section.

Usage:
    python application.py                     # ~/.oci/config DEFAULT profile
    python application.py -cp MYPROFILE       # another profile
    python application.py -ip                 # Instance Principals (on an OCI compute instance)
    python application.py -dt                 # Delegation Token (OCI Cloud Shell, auto-detected)
    python application.py -st -cp MYSESSION   # Session auth, profile created by "oci session authenticate"
    python application.py --interactive       # Browser login via OCI CLI, creates/reuses session profile SESSION
    python application.py --interactive MYSES # same, with a custom session profile name
    python application.py -rg eu-frankfurt-1  # work in a specific region
    python application.py -c /root/prod       # work in a specific compartment (OCID, name or path)
    python application.py -log run.txt        # also write all output to a log file
"""

import sys

import oci

from ocimodules.functions import (
    login,
    input_command_line,
    create_signer,
    check_oci_version,
    MyWriter,
    GetHomeRegion,
    GetTenantName,
    SubscribedRegions,
    GetFullCompartments,
    FindCompartment,
)

# Disable OCI CircuitBreaker feature (long running scripts should not be tripped by it)
oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()

#################################################
#           Application Configuration           #
#################################################
min_version_required = "2.88.0"
application_version = "26.10.28"


##########################################################################
# Main Program
##########################################################################
def main():
    check_oci_version(min_version_required)

    # Check command line parameters
    cmd = input_command_line()

    # if logging to file, overwrite default print function to also write to file
    writer = None
    if cmd.log_file != "":
        writer = MyWriter(sys.stdout, cmd.log_file)
        sys.stdout = writer

    try:
        print("Application Version: {}".format(application_version))

        #################################################
        # oci config and "login" check
        #################################################
        config, signer = create_signer(cmd.config_profile, cmd.is_instance_principals, cmd.is_delegation_token, cmd.is_security_token,
                                       interactive=(cmd.interactive_profile != ""), region=cmd.region)
        tenant_id = config['tenancy']

        login(config, signer)
        tenant_name = GetTenantName(config, signer)
        print("Tenant Name: {} ({})".format(tenant_name, tenant_id))

        #################################################
        # Regions
        #################################################
        home_region = GetHomeRegion(config, signer)
        regions = SubscribedRegions(config, signer)
        print("Home Region: {}".format(home_region))
        print("Subscribed Regions: {}".format(", ".join(regions)))

        # Optional region override from the command line (-rg)
        if cmd.region:
            if cmd.region not in regions:
                print("Region '{}' is not subscribed in this tenancy, aborting".format(cmd.region))
                sys.exit(1)
            config["region"] = cmd.region
        print("Working Region: {}".format(config["region"]))

        #################################################
        # Compartments
        #################################################
        compartments = GetFullCompartments(config, signer)
        print("Compartments found: {}".format(len(compartments)))

        # Optional compartment filter from the command line (-c): OCID, name or full path
        selected_compartment = None
        if cmd.compartment:
            selected_compartment = FindCompartment(compartments, cmd.compartment)
            if selected_compartment is None:
                print("Compartment '{}' not found, aborting".format(cmd.compartment))
                sys.exit(1)
            print("Working Compartment: {} ({})".format(selected_compartment.fullpath, selected_compartment.details.id))

        ##########################################################################
        # Your code starts here
        #
        # Examples:
        #   compute = oci.core.ComputeClient(config, signer=signer)
        #   for comp in compartments:
        #       instances = oci.pagination.list_call_get_all_results(
        #           compute.list_instances, compartment_id=comp.details.id).data
        #       print(comp.fullpath, len(instances))
        ##########################################################################
        for comp in compartments:
            print("{}{}".format("  " * comp.level, comp.fullpath))

    finally:
        if writer is not None:
            sys.stdout = writer.stdout
            writer.close()


if __name__ == "__main__":
    main()
