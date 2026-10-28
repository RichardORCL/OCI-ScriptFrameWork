"""
Shared helper functions for OCI (Oracle Cloud Infrastructure) Python scripts.

This module handles the boilerplate that every OCI script needs:
command line parsing, authentication (config file / instance principals /
delegation token), tenancy and region discovery, compartment tree discovery
and optional logging to file.
"""

import argparse
import configparser
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

import oci

# Default profile name used by --interactive (browser based session login)
DefaultSessionProfile = "SESSION"

# Seconds to wait before retrying an API call that returned HTTP 429 (Too Many Requests)
WaitRefresh = 10

# Maximum number of retries for HTTP 429 before giving up
MaxRetries = 10

# Warn once when get_owner() is used without owner tag keys in OCI config
_get_owner_config_warning_shown = False

# argparse const for -csv / -excel when path is omitted (see resolve_export_paths)
_EXPORT_USE_DEFAULT = "__EXPORT_USE_DEFAULT__"


class OCICompartments:
    """A compartment together with its full path (e.g. /root/prod/network) and depth level."""

    def __init__(self, details=None, fullpath="", level=0):
        self.details = details if details is not None else oci.identity.models.Compartment()
        self.fullpath = fullpath
        self.level = level

    def __repr__(self):
        return "OCICompartments(fullpath={!r}, level={}, id={!r})".format(
            self.fullpath, self.level, getattr(self.details, "id", None))


def _non_negative_int(value):
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def ensure_export_extension(path, extension):
    """Append .csv / .xlsx when the user omits the extension."""
    if not path.lower().endswith(extension.lower()):
        return path + extension
    return path


def resolve_export_paths(cmd, csv_default="", xlsx_default=""):
    """
    Turn argparse export flags into final paths on cmd (mutates cmd in place).

    After input_command_line(export_options=True), -csv / -excel with no path set
    csv_export / excel_export to _EXPORT_USE_DEFAULT; this applies script defaults.
    """
    if getattr(cmd, "csv_export", "") == _EXPORT_USE_DEFAULT:
        cmd.csv_export = csv_default
    elif getattr(cmd, "csv_export", ""):
        cmd.csv_export = ensure_export_extension(cmd.csv_export, ".csv")
    else:
        cmd.csv_export = ""

    if getattr(cmd, "excel_export", "") == _EXPORT_USE_DEFAULT:
        cmd.excel_export = xlsx_default
    elif getattr(cmd, "excel_export", ""):
        cmd.excel_export = ensure_export_extension(cmd.excel_export, ".xlsx")
    else:
        cmd.excel_export = ""

    return cmd


_LEADING_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)?)")


def _parse_excel_number(text, as_integer):
    """Parse a full number or a leading number (e.g. boot VPU '10 (auto)' -> 10)."""
    try:
        number = float(text)
    except (TypeError, ValueError):
        match = _LEADING_NUMBER_RE.match(text)
        if not match:
            return None
        number = float(match.group(1))
    if as_integer:
        return int(number)
    return number


def _excel_coerce_cell(column_key, value, int_columns, float_columns):
    """Return a value openpyxl can store as a number, or None/text for blanks."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and column_key in int_columns:
        return value
    if isinstance(value, float) and column_key in float_columns:
        return value

    text = str(value).strip()
    if text == "" or text == "-":
        return None

    if column_key in int_columns:
        parsed = _parse_excel_number(text, as_integer=True)
        if parsed is not None:
            return parsed
        return value
    if column_key in float_columns:
        parsed = _parse_excel_number(text, as_integer=False)
        if parsed is not None:
            return parsed
        return value
    return value


def write_excel_export_file(
    path,
    export_columns,
    rows,
    sheet_title="Export",
    int_columns=None,
    float_columns=None,
):
    """
    Write rows to an .xlsx file with numeric columns stored as Excel numbers.

    export_columns: list of (field_key, header_title) like EXPORT_COLUMNS in example scripts.
    rows: list of dicts keyed by field_key.
    int_columns / float_columns: sets of field_key names to coerce (blank and '-' become empty cells).
    """
    try:
        from openpyxl import Workbook
    except ImportError:
        print("ERROR: Excel export requires openpyxl. Install with: pip install openpyxl")
        sys.exit(1)

    int_columns = int_columns or frozenset()
    float_columns = float_columns or frozenset()
    field_keys = [key for key, _ in export_columns]

    wb = Workbook()
    ws = wb.active
    ws.title = (sheet_title or "Export")[:31]
    ws.append([title for _, title in export_columns])
    for row in rows:
        ws.append([
            _excel_coerce_cell(key, row.get(key, ""), int_columns, float_columns)
            for key in field_keys
        ])
    wb.save(path)


def _add_export_arguments(parser):
    parser.add_argument(
        "-csv",
        "--csv",
        nargs="?",
        const=_EXPORT_USE_DEFAULT,
        default="",
        dest="csv_export",
        metavar="FILE",
        help="Write tabular results to CSV (optional FILE; script default name if omitted)",
    )
    parser.add_argument(
        "-excel",
        "--excel",
        "-xlsx",
        "--xlsx",
        nargs="?",
        const=_EXPORT_USE_DEFAULT,
        default="",
        dest="excel_export",
        metavar="FILE",
        help="Write tabular results to Excel .xlsx (optional FILE; requires openpyxl)",
    )


##########################################################################
# input_command_line
##########################################################################
def input_command_line(help=False, export_options=False, min_age_option=False, disable_imds_v1_option=False):
    parser = argparse.ArgumentParser(formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=80, width=130))
    parser.add_argument('-cp', default="DEFAULT", dest='config_profile', help='Config Profile inside the config file')
    parser.add_argument('-ip', action='store_true', default=False, dest='is_instance_principals', help='Use Instance Principals for Authentication')
    parser.add_argument('-dt', action='store_true', default=False, dest='is_delegation_token', help='Use Delegation Token for Authentication')
    parser.add_argument('-st', action='store_true', default=False, dest='is_security_token', help='Use Session (Security Token) Authentication, created with "oci session authenticate"')
    parser.add_argument('-i', '--interactive', nargs='?', const=DefaultSessionProfile, default="", dest='interactive_profile', metavar='PROFILE',
                        help='Interactive browser login: create or reuse a session token via the OCI CLI ("oci session authenticate"). Optional session profile name, default: {}'.format(DefaultSessionProfile))
    parser.add_argument("-log", nargs='?', const='log.txt', default="", dest='log_file', help="Output also to logfile. If logfile not specified, will log to log.txt")
    parser.add_argument("-rg", default="", dest='region', help="Select Region")
    parser.add_argument("-c", default="", dest='compartment', help="Select Compartment (OCID, name or full path e.g. /root/prod)")
    if export_options:
        _add_export_arguments(parser)
    if min_age_option:
        parser.add_argument(
            "-minage",
            "--minage",
            type=_non_negative_int,
            default=0,
            metavar="DAYS",
            help="Only include resources at least DAYS old (from time_created)",
        )
    if disable_imds_v1_option:
        parser.add_argument(
            "-disable",
            "--disable",
            action="store_true",
            default=False,
            dest="disable_imds_v1",
            help="Disable IMDS v1 on matching instances (requires manage instance permission)",
        )
    cmd = parser.parse_args()

    # If running in Cloud Shell (OCI_CLI_CLOUD_SHELL=true), default to Delegation Token
    if os.environ.get("OCI_CLI_CLOUD_SHELL", "").lower() == "true":
        print("Running in Cloud Shell..")
        cmd.is_delegation_token = True
        cmd.is_instance_principals = False
        cmd.is_security_token = False
        cmd.interactive_profile = ""
        cmd.config_profile = "DEFAULT"

    # Interactive browser login: session token authentication using the chosen session profile
    elif cmd.interactive_profile:
        cmd.is_security_token = True
        cmd.is_instance_principals = False
        cmd.is_delegation_token = False
        cmd.config_profile = cmd.interactive_profile

    # If OCI_CLI_AUTH=security_token is set (same variable the OCI CLI uses), default to Session Authentication
    elif os.environ.get("OCI_CLI_AUTH", "").lower() == "security_token" and not (cmd.is_instance_principals or cmd.is_delegation_token):
        cmd.is_security_token = True

    if help:
        parser.print_help()

    return cmd


##########################################################################
# Create signer for Authentication
# Input - config_profile, is_instance_principals, is_delegation_token, is_security_token
#         interactive: create/reuse the session token via the OCI CLI before using it (implies is_security_token)
#         region: region to create the interactive session in (optional, CLI prompts if empty)
# Output - config and signer objects
##########################################################################
def create_signer(config_profile, is_instance_principals, is_delegation_token, is_security_token=False, interactive=False, region=""):

    if interactive:
        is_security_token = True
        ensure_interactive_session(config_profile, region)

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
    # Session / Security Token authentication
    # Profile created with: oci session authenticate [--profile-name NAME]
    # The profile contains security_token_file, key_file, tenancy and region (no user/fingerprint).
    # https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/clitoken.htm
    # -----------------------------
    elif is_security_token:
        try:
            config = oci.config.from_file(
                oci.config.DEFAULT_LOCATION,
                (config_profile if config_profile else oci.config.DEFAULT_PROFILE)
            )
            token_file = config["security_token_file"]
            with open(os.path.expanduser(token_file), 'r') as f:
                token = f.read().strip()

            private_key = oci.signer.load_private_key_from_file(
                config["key_file"],
                pass_phrase=oci.config.get_config_value_or_default(config, "pass_phrase")
            )

            # Check the token has not expired before making any API call
            token_container = oci.auth.security_token_container.SecurityTokenContainer(None, token)
            if not token_container.valid():
                print("The session token in profile '{}' has expired.".format(config_profile))
                print("Refresh it with:  oci session refresh --profile {}".format(config_profile))
                print("or re-authenticate: oci session authenticate --profile-name {}".format(config_profile))
                sys.exit(1)

            # Session profiles have no 'user' key; the user OCID is the 'sub' claim of the token
            if "user" not in config and token_container.get_jwt().get("sub"):
                config["user"] = token_container.get_jwt()["sub"]

            signer = oci.auth.signers.SecurityTokenSigner(token, private_key)
            return config, signer

        except KeyError as e:
            print("Profile '{}' is missing key {} - is this a session profile created with 'oci session authenticate'? aborting".format(config_profile, e))
            sys.exit(1)

        except Exception as e:
            print("Error obtaining session token authentication, aborting: {}".format(e))
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
# Session profile helpers (interactive browser login)
#################################################
def session_profile_status(profile):
    """
    Inspect a profile in ~/.oci/config.
    Returns (status, expiry) where status is one of:
      "missing"     - profile does not exist
      "not_session" - profile exists but is a regular API key profile (no security_token_file)
      "expired"     - session profile whose token has expired (or token file is unreadable)
      "valid"       - session profile with a valid token
    expiry is a datetime (local time) for session profiles, else None.
    """
    try:
        config = oci.config.from_file(oci.config.DEFAULT_LOCATION, profile)
    except (oci.exceptions.ConfigFileNotFound, oci.exceptions.ProfileNotFound):
        return "missing", None

    if "security_token_file" not in config:
        return "not_session", None

    try:
        with open(os.path.expanduser(config["security_token_file"]), 'r') as f:
            token = f.read().strip()
        container = oci.auth.security_token_container.SecurityTokenContainer(None, token)
        expiry = datetime.fromtimestamp(container.get_jwt()["exp"])
        return ("valid" if container.valid() else "expired"), expiry
    except Exception:
        return "expired", None


def ensure_interactive_session(profile, region=""):
    """
    Make sure a valid session token exists for `profile`, using the OCI CLI:
      - reuse it if still valid
      - try 'oci session refresh' if expired (works up to 24h after login)
      - otherwise run 'oci session authenticate' (opens a browser for login)
    Refuses to touch a profile that is not a session profile. Exits on failure.
    """
    oci_cli = shutil.which("oci")
    if oci_cli is None:
        print("Interactive login requires the OCI CLI ('oci' not found in PATH).")
        print("Install it: https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm  (or: pip install oci-cli)")
        sys.exit(1)

    status, expiry = session_profile_status(profile)

    if status == "not_session":
        print("Profile '{}' exists in {} but is not a session profile - refusing to overwrite it.".format(profile, oci.config.DEFAULT_LOCATION))
        print("Choose another session profile name, e.g.:  --interactive MYSESSION")
        sys.exit(1)

    if status == "valid":
        print("Reusing session profile '{}' (token valid until {:%Y-%m-%d %H:%M:%S})".format(profile, expiry))
        return

    if status == "expired":
        print("Session profile '{}' has expired, trying to refresh..".format(profile))
        result = subprocess.run([oci_cli, "session", "refresh", "--profile", profile])
        status, expiry = session_profile_status(profile)
        if result.returncode == 0 and status == "valid":
            print("Session refreshed (token valid until {:%Y-%m-%d %H:%M:%S})".format(expiry))
            return
        print("Refresh not possible, a new browser login is required.")

    print("Starting browser login for session profile '{}'..".format(profile))
    command = [oci_cli, "session", "authenticate", "--profile-name", profile]
    if region:
        command += ["--region", region]
    result = subprocess.run(command)
    if result.returncode != 0:
        print("'oci session authenticate' failed (exit code {}), aborting".format(result.returncode))
        sys.exit(1)

    status, expiry = session_profile_status(profile)
    if status != "valid":
        print("Session profile '{}' is still not usable after login (status: {}), aborting".format(profile, status))
        sys.exit(1)
    print("Session created (token valid until {:%Y-%m-%d %H:%M:%S})".format(expiry))


#################################################
#                 Login
#################################################
def login(config, signer):
    """Print SDK version and the identity we are connected with. Returns the user name (or None if unknown)."""
    print("OCI SDK Version: {}".format(oci.__version__))

    # Instance Principals / Delegation Token have no user (session auth gets it from the token in create_signer)
    user_id = config.get("user")
    if user_id is None:
        print("Logged in as: {} @ {}".format("Instance Principal / Delegation Token", config["region"]))
        return None

    identity = oci.identity.IdentityClient(config, signer=signer)
    try:
        user = identity.get_user(user_id=user_id).data
        print("Logged in as: {} @ {}".format(user.name, config["region"]))
        return user.name
    except oci.exceptions.ServiceError as e:
        print("Logged in as: {} @ {} (could not read user: {})".format(user_id, config["region"], e.message))
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
def _owner_tag_keys_from_config_dict(config):
    if not config:
        return None
    namespace = config.get("owner_namespace")
    owner_key = config.get("owner_name")
    created_key = config.get("owner_created")
    if namespace and owner_key and created_key:
        return {
            "owner_namespace": str(namespace).strip(),
            "owner_name": str(owner_key).strip(),
            "owner_created": str(created_key).strip(),
        }
    return None


def _owner_tag_keys_from_config_file(config_profile=None):
    config_path = os.path.expanduser(oci.config.DEFAULT_LOCATION)
    profile = config_profile or oci.config.DEFAULT_PROFILE
    if not os.path.isfile(config_path):
        return None

    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    if not parser.has_section(profile):
        return None

    section = parser[profile]
    namespace = section.get("owner_namespace")
    owner_key = section.get("owner_name")
    created_key = section.get("owner_created")
    if namespace and owner_key and created_key:
        return {
            "owner_namespace": namespace.strip(),
            "owner_name": owner_key.strip(),
            "owner_created": created_key.strip(),
        }
    return None


def _warn_owner_tag_config_missing():
    global _get_owner_config_warning_shown
    if _get_owner_config_warning_shown:
        return
    _get_owner_config_warning_shown = True
    print("WARNING: get_owner: owner tag mapping is not configured in the OCI config file ({}).".format(
        os.path.expanduser(oci.config.DEFAULT_LOCATION)
    ))
    print("Add to your profile, for example:")
    print("  owner_namespace=Mandatory_Tags")
    print("  owner_name=Owner")
    print("  owner_created=CreatedOn")


def get_owner(defined_tags, config=None, config_profile=None):
    """
    Read owner and created-on values from predefined (defined) tags on a resource.

    Tag key names and namespace are taken from the active OCI config profile:
      owner_namespace  - defined-tags namespace (e.g. Mandatory_Tags)
      owner_name       - tag key for owner (e.g. Owner)
      owner_created    - tag key for created date (e.g. CreatedOn)

    Parameters
    ----------
    defined_tags : dict
        Resource defined_tags as returned by OCI APIs (namespace -> {key: value}).
    config : dict, optional
        Config dict from create_signer(); owner_* keys are used when present.
    config_profile : str, optional
        Profile in ~/.oci/config when owner_* keys are not in `config` (default: DEFAULT).

    Returns
    -------
    dict
        {'owner': str or None, 'created_on': str or None}
    """
    mapping = _owner_tag_keys_from_config_dict(config)
    if mapping is None:
        mapping = _owner_tag_keys_from_config_file(config_profile)

    if mapping is None:
        _warn_owner_tag_config_missing()
        return {"owner": None, "created_on": None}

    defined_tags = defined_tags or {}
    ns_tags = defined_tags.get(mapping["owner_namespace"]) or {}

    return {
        "owner": ns_tags.get(mapping["owner_name"]),
        "created_on": ns_tags.get(mapping["owner_created"]),
    }


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
