"""
Shared helper functions for OCI (Oracle Cloud Infrastructure) Python scripts.

This module handles the boilerplate that every OCI script needs:
command line parsing, authentication (config file / instance principals /
delegation token), tenancy and region discovery, compartment tree discovery
and optional logging to file.
"""

import argparse
import os
import sys
import time

import oci

# Seconds to wait before retrying an API call that returned HTTP 429 (Too Many Requests)
WaitRefresh = 10

# Maximum number of retries for HTTP 429 before giving up
MaxRetries = 10


class OCICompartments:
    """A compartment together with its full path (e.g. /root/prod/network) and depth level."""

    def __init__(self, details=None, fullpath="", level=0):
        self.details = details if details is not None else oci.identity.models.Compartment()
        self.fullpath = fullpath
        self.level = level

    def __repr__(self):
        return "OCICompartments(fullpath={!r}, level={}, id={!r})".format(
            self.fullpath, self.level, getattr(self.details, "id", None))


##########################################################################
# input_command_line
##########################################################################
def input_command_line(help=False):
    parser = argparse.ArgumentParser(formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=80, width=130))
    parser.add_argument('-cp', default="DEFAULT", dest='config_profile', help='Config Profile inside the config file')
    parser.add_argument('-ip', action='store_true', default=False, dest='is_instance_principals', help='Use Instance Principals for Authentication')
    parser.add_argument('-dt', action='store_true', default=False, dest='is_delegation_token', help='Use Delegation Token for Authentication')
    parser.add_argument("-log", nargs='?', const='log.txt', default="", dest='log_file', help="Output also to logfile. If logfile not specified, will log to log.txt")
    parser.add_argument("-rg", default="", dest='region', help="Select Region")
    parser.add_argument("-c", default="", dest='compartment', help="Select Compartment (OCID, name or full path e.g. /root/prod)")
    cmd = parser.parse_args()

    # If running in Cloud Shell (OCI_CLI_CLOUD_SHELL=true), default to Delegation Token
    if os.environ.get("OCI_CLI_CLOUD_SHELL", "").lower() == "true":
        print("Running in Cloud Shell..")
        cmd.is_delegation_token = True
        cmd.is_instance_principals = False
        cmd.config_profile = "DEFAULT"

    if help:
        parser.print_help()

    return cmd


##########################################################################
# Create signer for Authentication
# Input - config_profile and is_instance_principals and is_delegation_token
# Output - config and signer objects
##########################################################################
def create_signer(config_profile, is_instance_principals, is_delegation_token):

    # -----------------------------
    # Instance Principals
    # -----------------------------
    if is_instance_principals:
        try:
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            config = {'region': signer.region, 'tenancy': signer.tenancy_id}
            return config, signer

        except Exception as e:
            print("Error obtaining instance principals certificate, aborting: {}".format(e))
            sys.exit(1)

    # -----------------------------
    # Delegation Token (OCI Cloud Shell)
    # -----------------------------
    elif is_delegation_token:

        try:
            # check if env variables OCI_CONFIG_FILE, OCI_CONFIG_PROFILE exist and use them
            env_config_file = os.environ.get('OCI_CONFIG_FILE')
            env_config_section = os.environ.get('OCI_CONFIG_PROFILE')

            if env_config_file is None or env_config_section is None:
                print("*** OCI_CONFIG_FILE and OCI_CONFIG_PROFILE env variables not found, abort. ***")
                print("")
                sys.exit(1)

            config = oci.config.from_file(env_config_file, env_config_section)
            delegation_token_location = config["delegation_token_file"]

            with open(delegation_token_location, 'r') as delegation_token_file:
                delegation_token = delegation_token_file.read().strip()

            # get signer from delegation token
            signer = oci.auth.signers.InstancePrincipalsDelegationTokenSigner(delegation_token=delegation_token)
            return config, signer

        except KeyError:
            print("* Key Error obtaining delegation_token_file from config profile, aborting")
            sys.exit(1)

        except Exception as e:
            print("Error obtaining delegation token authentication, aborting: {}".format(e))
            sys.exit(1)

    # -----------------------------
    # config file authentication (~/.oci/config)
    # -----------------------------
    else:
        try:
            config = oci.config.from_file(
                oci.config.DEFAULT_LOCATION,
                (config_profile if config_profile else oci.config.DEFAULT_PROFILE)
            )
            oci.config.validate_config(config)
            signer = oci.signer.Signer(
                tenancy=config["tenancy"],
                user=config["user"],
                fingerprint=config["fingerprint"],
                private_key_file_location=config.get("key_file"),
                pass_phrase=oci.config.get_config_value_or_default(config, "pass_phrase"),
                private_key_content=config.get("key_content")
            )
        except Exception as e:
            print("Error obtaining authentication, did you configure the config file? aborting: {}".format(e))
            sys.exit(1)

        return config, signer


#################################################
#                 Login
#################################################
def login(config, signer):
    """Print SDK version and the identity we are connected with. Returns the user name (or None for IP/DT)."""
    print("OCI SDK Version: {}".format(oci.__version__))

    # Instance Principals / Delegation Token configs have no 'user' key
    if "user" not in config:
        print("Logged in as: {} @ {}".format("Instance Principal / Delegation Token", config["region"]))
        return None

    identity = oci.identity.IdentityClient(config, signer=signer)
    try:
        user = identity.get_user(user_id=config["user"]).data
        print("Logged in as: {} @ {}".format(user.name, config["region"]))
        return user.name
    except oci.exceptions.ServiceError as e:
        print("Logged in as: {} @ {} (could not read user: {})".format("unknown", config["region"], e.message))
        return None


#################################################
#              GetHomeRegion
#################################################
def GetHomeRegion(config, signer):
    home_region = ""
    identity = oci.identity.IdentityClient(config, signer=signer)
    regionDetails = identity.list_region_subscriptions(tenancy_id=config["tenancy"]).data

    # Set home region for connection
    for reg in regionDetails:
        if reg.is_home_region:
            home_region = str(reg.region_name)

    return home_region


#################################################
#              GetTenantName
#################################################
def GetTenantName(config, signer):
    identity = oci.identity.IdentityClient(config, signer=signer)
    tenancy = identity.get_tenancy(config['tenancy']).data
    return tenancy.name


#################################################
#              SubscribedRegions
#################################################
def SubscribedRegions(config, signer):
    regions = []
    identity = oci.identity.IdentityClient(config, signer=signer)
    regionDetails = identity.list_region_subscriptions(tenancy_id=config["tenancy"]).data

    # Add subscribed regions to list
    for detail in regionDetails:
        regions.append(detail.region_name)

    return regions


#################################################
# Basic Get Compartments Function
# subtree=True is only valid when rootID is the tenancy OCID and
# returns the whole compartment hierarchy in a single (paginated) call.
#################################################
def GetCompartments(identity, rootID, subtree=False):
    for attempt in range(MaxRetries):
        try:
            compartments = oci.pagination.list_call_get_all_results(
                identity.list_compartments,
                compartment_id=rootID,
                compartment_id_in_subtree=subtree,
                access_level="ANY",
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY
            ).data
            return compartments
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                print("API busy.. retry {}/{}".format(attempt + 1, MaxRetries), end="\r")
                time.sleep(WaitRefresh)
            else:
                print("Error listing compartments for {}: {}".format(rootID, e.message))
                return []
    print("Giving up listing compartments for {} after {} retries".format(rootID, MaxRetries))
    return []


#################################################
# Get compartments for the tenant
# Returns a list of OCICompartments (root first, then sorted by full path)
# containing only ACTIVE compartments whose parents are also ACTIVE/accessible.
#################################################
def GetFullCompartments(config, signer):
    identity = oci.identity.IdentityClient(config, signer=signer)
    tenancy_id = config["tenancy"]

    # Synthetic root compartment entry
    root = oci.identity.models.Compartment()
    root.id = tenancy_id
    root.name = "root compartment"
    root.lifecycle_state = "ACTIVE"
    root.compartment_id = None

    result = [OCICompartments(root, "/root", 0)]

    # One call for the whole hierarchy instead of one call per compartment
    all_compartments = GetCompartments(identity, tenancy_id, subtree=True)
    active = {comp.id: comp for comp in all_compartments if comp.lifecycle_state == "ACTIVE"}

    # Resolve each compartment's path by walking up to the root (depth is at most 6)
    paths = {tenancy_id: ("/root", 0)}

    def resolve(compartment_id):
        if compartment_id in paths:
            return paths[compartment_id]
        comp = active.get(compartment_id)
        if comp is None:
            # parent is deleted or not accessible to this identity
            return None
        parent = resolve(comp.compartment_id)
        if parent is None:
            return None
        path = (parent[0] + "/" + comp.name, parent[1] + 1)
        paths[compartment_id] = path
        return path

    for compartment_id, comp in active.items():
        resolved = resolve(compartment_id)
        if resolved is not None:
            result.append(OCICompartments(comp, resolved[0], resolved[1]))

    # Sort hierarchically by path components, root stays first
    result.sort(key=lambda c: c.fullpath.split("/"))
    return result


#################################################
# Resolve Compartment OCID to Full Path using the full list of compartments
#################################################
def GetCompartmentFullPath(compartments, ocid):
    """
    Given a list of OCICompartments objects and an OCID,
    returns the full path of the compartment that matches the given OCID.
    If not found, returns None.
    """
    for compartment in compartments:
        if hasattr(compartment, "details") and getattr(compartment.details, "id", None) == ocid:
            return getattr(compartment, "fullpath", None)
    return None


#################################################
# Find a compartment by OCID, name or full path
#################################################
def FindCompartment(compartments, value):
    """
    Given a list of OCICompartments objects and a value that is either an OCID,
    a full path (e.g. /root/prod/network) or a compartment name,
    returns the first matching OCICompartments object or None.
    """
    for compartment in compartments:
        if compartment.details.id == value or compartment.fullpath == value:
            return compartment
    for compartment in compartments:
        if compartment.details.name == value:
            return compartment
    return None


##########################################################################
# Checking SDK Version
# Minimum version requirements for OCI SDK
##########################################################################
def check_oci_version(min_oci_version_required):
    outdated = False

    for i, rl in zip(oci.__version__.split("."), min_oci_version_required.split(".")):
        if int(i) > int(rl):
            break
        if int(i) < int(rl):
            outdated = True
            break

    if outdated:
        print("Your version of the OCI SDK is out-of-date. Please first upgrade your OCI SDK Library by running the command:")
        print("OCI SDK Version : {}".format(oci.__version__))
        print("Min SDK required: {}".format(min_oci_version_required))
        print("pip install --upgrade oci")
        sys.exit(1)


#############################################
# MyWriter to redirect output
#############################################
def CurrentTimeString():
    return time.strftime("%D %H:%M:%S", time.localtime())


class MyWriter:
    """
    Tee-style writer: everything written to it goes to the original stdout
    and is appended to a log file. Install with `sys.stdout = MyWriter(sys.stdout, "log.txt")`.
    """

    def __init__(self, stdout, filename):
        self.stdout = stdout
        self.filename = filename
        self.logfile = open(self.filename, "a", encoding="utf-8")

    def write(self, text):
        self.stdout.write(text)
        self.logfile.write(text)

    def flush(self):
        self.stdout.flush()
        self.logfile.flush()

    def close(self):
        # Only close the log file - never the real stdout
        self.flush()
        self.logfile.close()

    def isatty(self):
        return self.stdout.isatty()

    def __getattr__(self, name):
        # Delegate anything else (encoding, fileno, ...) to the real stdout
        return getattr(self.stdout, name)
