# OCI Python Framework

A small, dependency-light base template for writing scripts against **Oracle Cloud Infrastructure (OCI)** with the [OCI Python SDK](https://github.com/oracle/oci-python-sdk).

Every OCI script needs the same boilerplate: parse command-line options, authenticate (API key, Instance Principals or Cloud Shell delegation token), find the tenancy, regions and compartments, and optionally log output to a file. This repository provides that boilerplate so a new script is just "copy `application.py`, add your logic".

## Features

- Four authentication methods, selected from the command line:
  - API key from `~/.oci/config` (any profile)
  - Session / token-based authentication (browser login, no API key needed) - either fully integrated via `--interactive` or with an existing `oci session authenticate` profile
  - Instance Principals (when running on an OCI compute instance)
  - Delegation Token (OCI Cloud Shell, auto-detected)
- Tenancy name, home region and subscribed regions discovery
- Full compartment tree in a single API call, with human-readable paths such as `/root/prod/network`
- Compartment lookup by OCID, name or full path (`-c` option)
- Region override (`-rg` option)
- Optional tee-logging of all output to a file (`-log` option)
- OCI SDK minimum version check
- Automatic retry on HTTP 429 (API throttling)

## Requirements

- Python 3.8 or newer
- OCI Python SDK 2.88.0 or newer

```bash
pip install -r requirements.txt
```

## Authentication setup

### 1. API key (default)

Create an API signing key and config file as described in the [OCI documentation](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm). A typical `~/.oci/config` looks like:

```ini
[DEFAULT]
user=ocid1.user.oc1..aaaa...
fingerprint=aa:bb:cc:...
tenancy=ocid1.tenancy.oc1..aaaa...
region=eu-frankfurt-1
key_file=~/.oci/oci_api_key.pem
```

Use `-cp PROFILE` to select a profile other than `DEFAULT`.

Optional tag-mapping keys (used by `get_owner()` in scripts):

```ini
owner_namespace=Mandatory_Tags
owner_name=Owner
owner_created=CreatedOn
```

### 2. Session (token-based) authentication

Useful when you cannot or do not want to create an API key, e.g. with federated / SSO users. A browser opens, you log in, and a short-lived session token (1 hour, refreshable up to 24 hours) is stored in a profile in `~/.oci/config`. See [Token-based Authentication for the CLI](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/clitoken.htm). Requires the [OCI CLI](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm) (`pip install oci-cli`).

**Integrated (recommended):**

```bash
python application.py --interactive                         # session profile "SESSION"
python application.py --interactive MYSESSION -rg eu-frankfurt-1
```

`--interactive` (or `-i`) takes care of the whole session lifecycle:

1. If the session profile exists and the token is still valid, it is reused (no browser).
2. If the token has expired, `oci session refresh` is tried first (silent, works up to 24 h after login).
3. Otherwise `oci session authenticate` is run and a browser opens for login. Pass `-rg REGION` to skip the CLI's region prompt.

It refuses to overwrite a profile that is not a session profile, so an existing API-key profile with the same name is safe.

**Manual:**

```bash
oci session authenticate --profile-name MYSESSION --region eu-frankfurt-1
python application.py -st -cp MYSESSION
```

Setting `OCI_CLI_AUTH=security_token` (the variable the OCI CLI uses) enables `-st` automatically. In both variants the script checks the token's expiry before calling any API.

### 3. Instance Principals

Run the script on an OCI compute instance that belongs to a dynamic group with the required IAM policies, and pass `-ip`.

### 4. Delegation Token (Cloud Shell)

Open [OCI Cloud Shell](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/cloudshellintro.htm) and run the script. Cloud Shell is detected automatically via the `OCI_CLI_CLOUD_SHELL` environment variable; `-dt` forces this mode.

## Usage

```bash
python application.py [options]
```

| Option | Description |
| --- | --- |
| `-cp PROFILE` | Config profile inside `~/.oci/config` (default: `DEFAULT`) |
| `-ip` | Use Instance Principals for authentication |
| `-dt` | Use Delegation Token for authentication (Cloud Shell) |
| `-st` | Use Session (security token) authentication with an existing session profile; combine with `-cp` to pick it |
| `-i [PROFILE]`, `--interactive [PROFILE]` | Browser login via the OCI CLI: create, refresh or reuse a session token in the given session profile (default: `SESSION`) |
| `-rg REGION` | Work in a specific subscribed region, e.g. `eu-frankfurt-1` |
| `-c COMPARTMENT` | Work in a specific compartment. Accepts an OCID, a name or a full path such as `/root/prod` |
| `-log [FILE]` | Also write all output to a log file (default file name: `log.txt`) |
| `-h` | Show help |

Example run:

```text
$ python application.py -c /root/prod -log
Application Version: 26.10.28
OCI SDK Version: 2.189.0
Logged in as: jane.doe@example.com @ eu-frankfurt-1
Tenant Name: mytenancy (ocid1.tenancy.oc1..aaaa...)
Home Region: eu-frankfurt-1
Subscribed Regions: eu-frankfurt-1, eu-amsterdam-1
Working Region: eu-frankfurt-1
Compartments found: 12
Working Compartment: /root/prod (ocid1.compartment.oc1..aaaa...)
/root
  /root/prod
    /root/prod/network
...
```

## Creating a new script

1. Copy `application.py` to a new file (e.g. `list_instances.py`).
2. Keep the top part (version check, command line, login, regions, compartments).
3. Replace the block under `# Your code starts here` with your logic. `config`, `signer`, `compartments`, `regions` and `selected_compartment` are ready to use:

```python
compute = oci.core.ComputeClient(config, signer=signer)
targets = [selected_compartment] if selected_compartment else compartments
for comp in targets:
    instances = oci.pagination.list_call_get_all_results(
        compute.list_instances, compartment_id=comp.details.id).data
    for inst in instances:
        print("{:<40} {:<30} {}".format(comp.fullpath, inst.display_name, inst.lifecycle_state))
```

To work across all subscribed regions, loop over `regions` and set `config["region"]` before creating each client.

## Functions available in `ocimodules/functions.py`

### Command line and authentication

| Function | Description |
| --- | --- |
| `input_command_line(help=False)` | Parses the standard options (`-cp`, `-ip`, `-dt`, `-st`, `-i/--interactive`, `-rg`, `-c`, `-log`) and auto-detects Cloud Shell (`OCI_CLI_CLOUD_SHELL`) and session auth (`OCI_CLI_AUTH=security_token`). Returns an `argparse.Namespace`. |
| `create_signer(config_profile, is_instance_principals, is_delegation_token, is_security_token=False, interactive=False, region="")` | Builds the OCI `config` dict and `signer` for the selected authentication method. For session auth it verifies the token has not expired and fills `config["user"]` from the token; with `interactive=True` it first calls `ensure_interactive_session`. Exits with a message on failure. |
| `ensure_interactive_session(profile, region="")` | Guarantees a valid session token for `profile` using the OCI CLI: reuse → `oci session refresh` → `oci session authenticate` (browser). Refuses to overwrite non-session profiles. |
| `session_profile_status(profile)` | Returns `("missing" / "not_session" / "expired" / "valid", expiry_datetime)` for a profile in `~/.oci/config`. |
| `login(config, signer)` | Prints the SDK version and the identity in use. Returns the user name, or `None` for Instance Principals / Delegation Token. |
| `check_oci_version(min_version)` | Exits with an upgrade hint if the installed OCI SDK is older than `min_version`. |

### Tenancy and regions

| Function | Description |
| --- | --- |
| `GetTenantName(config, signer)` | Returns the tenancy display name. |
| `GetHomeRegion(config, signer)` | Returns the home region name (e.g. `eu-frankfurt-1`). |
| `SubscribedRegions(config, signer)` | Returns a list of all region names the tenancy is subscribed to. |

### Compartments

| Function / Class | Description |
| --- | --- |
| `OCICompartments` | Simple container with `details` (the SDK `Compartment` model), `fullpath` (e.g. `/root/prod/network`) and `level` (0 for root). |
| `GetFullCompartments(config, signer)` | Returns the whole compartment tree as a list of `OCICompartments`, root first, sorted by path. Only `ACTIVE` compartments are returned. Uses one `list_compartments(compartment_id_in_subtree=True)` call. |
| `GetCompartments(identity, root_id, subtree=False)` | Low-level: lists the child compartments of `root_id` (all pages). `subtree=True` is only valid for the tenancy OCID. Retries on HTTP 429. |
| `GetCompartmentFullPath(compartments, ocid)` | Resolves a compartment OCID to its full path using the list from `GetFullCompartments`. Returns `None` if not found. |
| `FindCompartment(compartments, value)` | Finds a compartment by OCID, full path or name. Returns an `OCICompartments` or `None`. |

### Tags / owner (defined tags)

| Function | Description |
| --- | --- |
| `get_owner(defined_tags, config=None, config_profile=None)` | Reads **owner** and **created_on** from a resource's `defined_tags`, using namespace/key names from the OCI config profile: `owner_namespace`, `owner_name`, `owner_created`. Returns `{'owner': ..., 'created_on': ...}`. Prints a **one-time warning** if those keys are missing from the config file. |

Optional entries in `~/.oci/config` (same profile as `-cp`). The values below are **examples only**—set `owner_namespace`, `owner_name`, and `owner_created` to match the defined-tag namespace and tag keys used for mandatory tags (or your owner metadata) in **your** tenancy:

```ini
owner_namespace=Mandatory_Tags
owner_name=Owner
owner_created=CreatedOn
```

Example:

```python
info = get_owner(volume.defined_tags, config=config, config_profile=cmd.config_profile)
print(info["owner"], info["created_on"])
```

### Logging

| Function / Class | Description |
| --- | --- |
| `MyWriter(stdout, filename)` | Tee writer: install with `sys.stdout = MyWriter(sys.stdout, "log.txt")` so every `print()` also lands in the file. Call `.close()` at the end. |
| `CurrentTimeString()` | Returns the current local time as `MM/DD/YY HH:MM:SS`, handy for log lines. |

## Security notes

- No credentials are stored in this repository. Authentication material lives in `~/.oci/config`, the key file it points to, the session token under `~/.oci/sessions/`, the instance metadata service, or the Cloud Shell delegation token.
- Session tokens are short-lived (1 hour, refreshable for up to 24 hours) and are a good fit for interactive use with SSO / federated identities, as no long-lived API key has to be created.
- `.gitignore` excludes `config`, `*.pem`, `*.key`, token files and log output so they are not committed by accident. Review log files before sharing them: they contain tenancy and compartment OCIDs and resource names.
- The scripts only need read permissions for the template itself (`inspect compartments`, `read users`, `inspect tenancies`, `read region-subscriptions`). Grant additional permissions per script, following least privilege.
- Keep the OCI SDK up to date (`pip install --upgrade oci`); it pulls in `cryptography`, which receives security fixes regularly.

## Contributing

Issues and pull requests are welcome. Please keep helper functions generic (no script-specific logic in `ocimodules/functions.py`).

## License

Licensed under the [Universal Permissive License (UPL) 1.0](LICENSE.txt).
