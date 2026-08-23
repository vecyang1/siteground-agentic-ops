# Novamira Update Contract

`siteground_ops.novamira_update.NovamiraUpdater` is the fail-closed update
boundary for the local Novamira CLI. It separates an unattended version check
from an explicitly confirmed package mutation.

## Check lane

`NovamiraUpdater.check()` is safe to run unattended. It reads the published
release, installed package metadata, and the local `novamira-ops` guidance. It
does not inspect profile credentials, create a snapshot, invoke Bun, install a
package, or run rollback. Its receipt reports `mutation_state: not_applicable`
and lists blockers without exposing secrets.

If the owner baseline is missing, check remains blocked. A human must first
review the current verified package and run the explicit
`novamira-update baseline --confirm-version 1.0.3` command once; the unattended
LaunchAgent never initializes this trust anchor.

## Apply gates

`apply(confirmed=True)` may proceed only when all of these are true:

- the candidate is exactly the reviewed `@novamira/cli` `1.0.3` contract;
- registry integrity and provenance checks pass;
- the installed package is exactly `1.0.0` and its files are clean;
- the package owner is the home `package.json` plus `bun.lock`;
- local guidance describes Novamira server `1.11.1+` and full-access OAuth,
  without the removed `--access read` route;
- the candidate staging check passes the pinned artifact integrity and offline doctor in the network-denying sandbox;
- the caller supplies explicit confirmation.

The updater passes an exact controlled Bun command to the backend. The backend
installs the already-reviewed tarball bytes from a temporary local artifact in
an owned Bun prefix (`--no-save`), then commits only the Novamira package tree.
It rewrites the home manifest and lockfile to the stable exact version spec and
must not leave the deleted artifact path in either file or refetch a mutable
registry URL:

```text
bun add --exact --ignore-scripts --no-save <pinned-novamira-cli-1.0.3.tgz>
```

The vendor shell installer and vendor skill installer are never called. The
local `novamira-ops` skill remains a separate owner and must not be overwritten.

## Snapshot and rollback

Before Bun runs, the backend snapshots the exact Novamira package and hoisted
dependency-tree file bytes plus executable modes, manifest, lockfile, and
previous CLI version, plus the owner baseline receipt. After installation it independently verifies
the installed version, package integrity, offline doctor, and unchanged profile
inventory. Only after those checks pass does it refresh the baseline digest for
the new manifest/lock bytes. Any verification failure restores the snapshot without refetching a
previous version. The receipt is `rolled_back` only after the previous version,
integrity, profile inventory, owner baseline, and owner-file/package readback all succeed;
otherwise it is `unknown` and must not be retried automatically. A process lock
serializes the complete apply/rollback transaction.

## Test gate

Run the focused and full suites with the source path enabled:

```bash
PYTHONPATH=src python3 -m pytest tests/test_novamira_update.py -q
PYTHONPATH=src python3 -m pytest -q
```

The updater tests use backend fakes for external registry, Bun, profile, and
doctor boundaries. They do not mutate the live package. A separate live proof
may call `LocalNovamiraBackend.registry_release()` and
`verify_candidate()`; an idempotent apply at the installed version must return
`result: up_to_date` without invoking Bun.
