# SiteGround Agentic Ops

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

`siteground-ops` is a small, fail-closed controller for the user's shared-hosted WordPress sites. It composes SiteGround's supported SSH/WP-CLI lane and WordPress-native readback instead of pretending Site Tools has a public API.

The long-lived local runtime is registered as `infra-siteground-novamira-cli-updater` in whatever service registry the operator keeps. That registry and this README are the cross-owner proof surfaces; the LaunchAgent is only the execution switch.

## Current verified profiles

The local owner at `~/.config/siteground-ops/sites.json` contains non-secret
pointers only. World Inspire uses its existing SSH/WP-CLI owner. Sites with an
exact `novamira-ops` MCP server can use that route for read-only diagnostics
when SSH is not configured, including Novamira-only profiles with no fake SSH
paths.

## Commands

```bash
siteground-ops sites
siteground-ops doctor <site-id> [--transport auto|ssh|novamira]
siteground-ops inventory <site-id> [--transport auto|ssh|novamira]
siteground-ops wp-admin <site-id-or-domain> [--app <id>] [--foreground]
siteground-ops portal read <account> wp-apps
siteground-ops cache-purge <site-id> --confirm-target <site-id> --recovery-receipt <receipt>
siteground-ops novamira-update check
siteground-ops novamira-update baseline --confirm-version 1.0.3
siteground-ops novamira-update apply --confirm-version 1.0.3
```

## WordPress admin sign-in

`wp-admin` signs into one exact WordPress application through SiteGround's own
one-click autologin — the mechanism behind the portal's "WordPress Admin"
button — using the signed-in Chrome profile. No WordPress password is involved
and none is stored. The tab opens in the background and stays open;
`--foreground` raises the window.

Targets are a profile id or a bare domain the portal serves. A profile whose
`public_url` is a customer-facing custom domain needs `portal_site_id` pinned,
which then takes priority over any domain match.

The application id is read from the portal, never assumed. Three live
behaviours make a positional guess wrong: the id is not always `1`
(`example-lang.com` is `3`); a site with a staging copy has more than one, which is
refused until `--app` names one; and every application reports the *site's*
domain, so `admin_host` — the host a login actually lands on — is carried
separately and a mismatch with the profile domain warns.

The minted URL is a single-use administrator credential. It is fetched,
validated for host and shape, and followed entirely inside the browser, so it
never reaches this process, stdout, a log, or a receipt; a contract test asserts
no command can return it. A login that cannot be confirmed is reported as
`unknown` with exit 3 and an explicit instruction not to repeat the command:
reads retry once on a dropped browser bridge, `wp-login` never does, because a
repeat mints a second credential rather than repeating a query.

The unattended lane is `update-novamira-cli-stable.sh`. It runs a read-only
check daily at 05:20 local time and auto-applies only the reviewed `1.0.3`
release when all gates pass. A newer or changed release is recorded as a
refusal until the supported version and its evidence are reviewed in code.
On a new machine, a missing owner baseline is refused; establish it once with
the explicit `baseline` command after reviewing the current package/manifest/lock.
Install or repair the LaunchAgent with `./install.sh`; inspect the receipt with
`./check-status.sh`; remove it with `./uninstall.sh`.

The current machine exposes the launcher at `~/.local/bin/siteground-ops` and
the config owner at `~/.config/siteground-ops/sites.json`. For another machine,
run the checked-in `bin/siteground-ops` wrapper from this repository with
`PYTHONPATH=src`, then create a profile from `config/sites.example.json` using
non-secret credential pointers. Keep the maintained source and the updater in
one checkout, because this is executable control-plane code; the human/agent
skill surfaces are symlinks to `skill/`, so there is still one canonical
implementation rather than a second copied skill folder.

Novamira reads also require the non-secret owner receipt at
`~/.config/siteground-ops/novamira-runtime.json` (shape:
`config/novamira-runtime.example.json`). It pins the reviewed
`@automattic/mcp-wordpress-remote@0.3.5` local runtime by exact command,
package identity/version, and runtime-tree SHA-256. `siteground-ops` injects
that command into the existing `novamira-ops` bridge, so the bridge's generic
`@latest` fallback is never used by this production path. A missing or changed
runtime, bridge, or exact MCP server makes the transport unavailable in
`siteground-ops sites` and fails closed. The current local receipt reuses the
existing npm cache in place instead of copying another 45 MB runtime.

Reads return one JSON receipt and name the transport that answered. `auto`
uses SSH only when its local files and required credential names are ready;
otherwise it may use the profile's exact `novamira_server`. It never switches
to Novamira after an SSH connection, host-key, authentication, timeout, or
remote-output failure. Mutations require exact target confirmation and a
recovery receipt. A timeout after a mutation remains `unknown`; the controller
does not retry it.

## Safety boundaries

- SSH uses Paramiko's system `known_hosts` and `RejectPolicy`; unknown or changed hosts fail closed.
- The WP-CLI surface is an explicit read/cache allowlist. There is no arbitrary shell or arbitrary WP-CLI command.
- The Novamira read adapter calls the skill-owned `wp_ops.py` with a fixed
  `doctor` or `inventory` PHP program, an exact server name, `shell=False`, a
  pinned verified MCP runtime, streaming stdout/stderr limits, a bounded
  timeout, and mandatory `home_url()` parity. It accepts no PHP or command text
  from config or CLI input and terminates the whole bridge process group on a
  timeout or output-limit breach.
- Novamira reports `siteground_cache_cli: null` because that route cannot prove
  the remote WP-CLI command. Its update lists are labeled
  `wordpress_cached_transients` with separate core/plugin/theme last-check
  timestamps; use SSH inventory when a fresh provider update check is required.
- Both inventory transports expose stable plugin/theme `id` fields. Human
  display names may differ, so automation must key on `id`, not `name`.
- Novamira is read-only here. `cache-purge` and every future mutation remain on
  the explicitly qualified SSH/WP-CLI route.
- Credentials remain in approved project env/1Password owners. Profiles contain pointers only.
- Novamira `1.0.3` removed read-scoped OAuth and requires full-access authorization. The updater reconciles the local `novamira-ops` guidance, verifies the exact npm tarball bytes with SRI plus npm's official signature/provenance verifier, stages the pinned artifact under a network-denying sandbox, installs it in an owned temporary Bun prefix without saving the temporary path, rewrites a stable exact manifest/lock spec, snapshots package/dependency bytes plus modes, checks offline doctor/profile identity with a scrubbed HOME, refreshes its owner baseline only after successful readback, and restores every owned path (including that baseline) on failure. It never updates the local skill.

## Provider boundary

SiteGround documents SSH/WP-CLI and `wp sg purge` on shared plans. Site Tools backups, staging, DNS, SSL, PHP settings, and mailbox provisioning remain UI-owned because no supported public account API/CLI was found. OpenCLI/Chrome is a bounded fallback for those controls only.

Run tests from this repository:

```bash
PYTHONPATH=src uv run --isolated --with pytest --with 'paramiko>=3.4,<5' -- python -m pytest -q
```

## License

Copyright (C) 2026 V

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along
with this program. If not, see <https://www.gnu.org/licenses/>.

The Affero clause rather than plain GPL because the plausible way this tool gets
taken proprietary is not a shipped binary — it is a hosted dashboard that runs a
modified copy and never distributes anything. AGPL section 13 reaches that case;
GPL-3.0 does not.

Third-party components keep their own terms and are compatible with this one:
[OpenCLI](https://github.com/jackwener/opencli) (Apache-2.0) is resolved at
runtime, and `paramiko` (LGPL-2.1-or-later) is an ordinary dependency. Neither
is vendored here.
