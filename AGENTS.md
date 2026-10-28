# AGENTS.md

Instructions for **AI coding agents** working in the [OCI Python Framework](README.md) repository. Follow this file so new scripts stay consistent, safe, and maintainable.

Human-oriented setup and auth are in **`README.md`**.

## What this repository is

- A **thin template** for OCI automation with the [OCI Python SDK](https://github.com/oracle/oci-python-sdk).
- Shared boilerplate lives in **`ocimodules/functions.py`** (auth, CLI, compartments, regions, logging, `get_owner()`).
- **`application.py`** is the minimal starter script users copy for one-off tools.
- **`examples/<service>/`** holds **complete, runnable** scripts that scan tenancies (often all subscribed regions). Use them as the primary reference when adding similar scripts.

Prefer extending this repo’s patterns over inventing new frameworks.

## Golden rules

1. **Do not put script-specific logic in `ocimodules/functions.py`.** Only add helpers there if they are generic and reusable across many OCI scripts. Otherwise keep logic in the script under `examples/` or a new top-level script.
2. **Never commit credentials.** No API keys, `config`, `.pem`, session tokens, or real tenancy OCIDs in code or docs. Output files (`*.csv`, `*.xlsx`, `*.log`) are gitignored—do not force-add them.
3. **Preserve the standard CLI** for every script that uses `input_command_line()`: `-cp`, `-ip`, `-dt`, `-st`, `-i` / `--interactive`, `-rg`, `-c`, `-log`, `-h`. Script-only flags must be stripped **before** calling `input_command_line()` (see below).
4. **Run scripts from the repository root** so imports and paths work:
   ```bash
   python examples/compute/list_imds_v1_instances.py
   ```
5. **Match existing style:** same auth/bootstrap block, `NoCircuitBreakerStrategy`, version check, optional `MyWriter`, compartment subtree filter for `-c`, and clear module docstring with usage examples.
6. **Minimize diff scope.** One focused script or helper per task; do not refactor unrelated examples unless the user asks.
7. **Commit and push only when the user explicitly asks.**

## Where to add code

| Goal | Location |
| --- | --- |
| New inventory / audit / report script | `examples/<area>/<descriptive_name>.py` (e.g. `examples/database/...`) |
| Quick single-region prototype | Copy `application.py` to a new file at repo root **or** add under `examples/` if it should be kept |
| Shared auth, compartments, owner tags | `ocimodules/functions.py` (generic only) |
| User-facing documentation | `README.md` (avoid duplicating long API lists—link to OCI docs when needed) |
| Agent-oriented conventions | This file (`AGENTS.md`) |

## Bootstrap pattern (required for `examples/` scripts)

Examples are not installed as a package. Insert the repo root on `sys.path` before importing `ocimodules`:

```python
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from ocimodules.functions import (
    login,
    input_command_line,
    create_signer,
    check_oci_version,
    # ... other helpers as needed
)

import oci
oci.circuit_breaker.GLOBAL_CIRCUIT_BREAKER_STRATEGY = oci.circuit_breaker.NoCircuitBreakerStrategy()
```

Set `MIN_OCI_VERSION = "2.88.0"` (or align with `requirements.txt`) and a `SCRIPT_VERSION` date string; call `check_oci_version(MIN_OCI_VERSION)` at the start of `main()`.

## Standard `main()` flow

1. Parse CLI (see next section).
2. Optional logging: `MyWriter(sys.stdout, cmd.log_file)` and restore stdout in a `finally` block.
3. `config, signer = create_signer(...)` with the same arguments as existing examples (`interactive=(cmd.interactive_profile != "")`, `region=cmd.region`).
4. `login(config, signer)`; print tenant via `GetTenantName`.
5. Regions: `SubscribedRegions`; if `cmd.region` is set, validate it is subscribed and scan **only** that region; else scan **all** subscribed regions (sorted).
6. Compartments: `GetFullCompartments`; if `cmd.compartment`, `FindCompartment` and filter to that subtree (copy `_instance_in_compartment_subtree` / `compartments_to_scan` from an example—do not reimplement differently).
7. Create **regional** SDK clients with `regional_config = dict(config); regional_config["region"] = region`.
8. Implement scan logic; handle `403`/`404` per compartment without aborting the whole run unless appropriate.
9. Print a summary table and optional export paths.

For a **single-region** script tied to `config["region"]` only, `application.py` is enough; for tenancy-wide reports, follow the **multi-region + ThreadPoolExecutor (one worker per region)** pattern in `examples/blockvolume/list_unattached_volumes.py` and siblings.

## Script-specific CLI flags (`parse_cmd`)

`input_command_line()` owns `argparse` for framework flags. If the script adds options (e.g. `-csv`, `-excel`, `-minage`, `-disable`):

1. Walk `sys.argv[1:]`, consume script-only flags and their values.
2. Append everything else to a filtered list.
3. Replace `sys.argv` with `[sys.argv[0]] + filtered`.
4. Call `cmd = input_command_line()` and attach extra attributes (`cmd.csv_export = ...`).

Do **not** register `-csv` on the global parser in `functions.py`.

## OCI API patterns agents should know

- **Pagination:** use `oci.pagination.list_call_get_all_results(...)` for list APIs.
- **Compartment scope:** many services require listing **per compartment OCID**. Do not assume `compartment_id_in_subtree` exists on every API (e.g. `compute.list_instances` does **not** support it—list per compartment from `GetFullCompartments`).
- **Regional endpoints:** Block Storage, Compute, Load Balancer, Object Storage, etc. need a client per region.
- **Object Storage namespace:** resolve once per tenancy with `get_namespace()` (home region client is typical).
- **Throttling (HTTP 429):** long scans in examples use retry strategies and sometimes per-region throttle flags; copy that pattern when adding heavy list/get loops rather than unbounded parallel calls in one region.
- **Service errors:** catch `oci.exceptions.ServiceError`; treat missing IAM in a compartment as skip, not fatal, unless the script’s purpose requires hard failure.

Verify parameter names against the installed SDK or [OCI API docs](https://docs.oracle.com/en-us/iaas/api/)—do not guess.

## Owner / mandatory tags

Scripts that show an **Owner** column should use:

```python
from ocimodules.functions import get_owner

info = get_owner(resource.defined_tags, config=config, config_profile=cmd.config_profile)
owner = info.get("owner") or "-"
```

Tag namespace and keys come from the user’s **`~/.oci/config`** profile (`owner_namespace`, `owner_name`, `owner_created`). Values in README are **examples**; they must match the tenancy’s defined-tag setup. Do not hardcode tag namespaces in script logic.

## Exports and optional dependencies

- **CSV:** standard library `csv`; default filenames like `report.csv`; use `_ensure_export_extension` pattern from block volume example.
- **Excel:** optional `openpyxl`; on `ImportError`, print install hint and `sys.exit(1)`.
- Exports are gitignored—generate them at runtime only.

## Reference examples (copy patterns from here)

| Script | Patterns to reuse |
| --- | --- |
| `examples/blockvolume/list_unattached_volumes.py` | Multi-region thread pool, `ScanProgress`, 429 retry, `-csv`/`-excel`/`-minage`, `get_owner`, age from `time_created` |
| `examples/compute/list_imds_v1_instances.py` | Per-compartment instance listing, mutating action flag (`-disable`) with throttle-aware updates |
| `examples/compute/list_windows_license_included.py` | Lighter scan, instance licensing fields |
| `examples/objectstorage/list_public_buckets.py` | `get_namespace`, list + get detail APIs |
| `examples/networking/list_load_balancer_tls_ciphers.py` | Listener-level reporting, SDK doc-derived predefined cipher maps |

When adding a script in a new folder under `examples/`, mirror docstring **Usage** lines and the same header/summary output style.

## Testing and verification

- Run `python -m py_compile path/to/script.py` after edits.
- If the environment has valid `~/.oci/config`, a dry run with `-rg <one region> -c <small compartment>` is ideal; never ask to commit config or keys.
- Do not claim a script works without running at least syntax compile (or a real run when credentials exist).

## IAM and security notes for generated scripts

- Document **minimum IAM** in the script docstring (e.g. read-only inspect/list/get for the relevant service).
- Avoid writing secrets to logs; `-log` captures printed output including resource names and OCIDs.
- Prefer read-only operations unless the user explicitly requests create/update/delete.

## Checklist for a new example script

- [ ] Path under `examples/<area>/` with clear name (`list_`, `report_`, etc.)
- [ ] Module docstring with purpose and example commands from repo root
- [ ] `_REPO_ROOT` + `ocimodules` import + circuit breaker disable
- [ ] Framework CLI preserved; custom flags via `parse_cmd` if needed
- [ ] `-c` compartment subtree filter when scanning many compartments
- [ ] Multi-region behavior consistent with other examples when the resource is regional
- [ ] `403`/`404` handled gracefully where appropriate
- [ ] No secrets, no unrelated refactors, no script-specific code in `functions.py`
- [ ] Update `README.md` only if the user wants user-facing docs changed (optional one-line mention in Features is enough when asked)

## Repository remote

Published copy: **https://github.com/RichardORCL/OCI-ScriptFrameWork** (`main`). Push only on user request.
