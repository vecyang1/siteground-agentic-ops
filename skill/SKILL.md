---
name: siteground-ops
description: Use for safe, auditable SiteGround shared-host WordPress operations, one-command wp-admin sign-in via the provider autologin, SSH/WP-CLI or exact-site Novamira MCP reads, guarded cache purge, and Novamira CLI maintenance.
---

# SiteGround Ops

Use the local `siteground-ops` CLI as the thin control plane for shared-hosted WordPress. It is designed for both humans and agents: explicit site IDs, JSON receipts, redacted diagnostics, read-first defaults, and refusal on ambiguous targets or unverified writes.

## First move

```bash
siteground-ops sites
siteground-ops doctor <exact-site-id> [--transport auto|ssh|novamira]
siteground-ops inventory <exact-site-id> [--transport auto|ssh|novamira]
```

Use the exact target returned by `sites`. `ready` reports the primary adapter;
`read_ready` and `read_transports` report the usable read paths. A false value
is not an authorization request; repair only non-secret pointers through the
approved project owner.

## Open wp-admin

```bash
siteground-ops wp-admin <site-id-or-domain> [--app <id>] [--foreground]
```

Signs into one exact WordPress application using SiteGround's own one-click
autologin, the same mechanism the portal's "WordPress Admin" button uses. No
WordPress password is involved and none is stored.

- The target is a profile id from `siteground-ops sites` **or** a bare domain the
  portal serves. A profile whose `public_url` is a customer-facing custom domain
  needs `portal_site_id` pinned, which then wins over any domain match.
- The tab opens in the **background** by default and stays open. `--foreground`
  raises the window; nothing else steals focus.
- Discover exact ids with `siteground-ops portal read <account> wp-apps`.

Three provider behaviours make guessing wrong here, and all three are live on
this account, so the command reads them rather than assuming:

- The application id is not always `1` — `example-lang.com` is `3`.
- A site with a staging copy has more than one application. That is refused with
  `ambiguous_wordpress_application` and the available ids until `--app` names one.
- Every application reports the *site's* domain, so a staging application reads
  as `example.com` while its admin is `staging2.example.com`. `admin_host` is the
  host a login actually lands on, and opening one that differs from the profile
  domain emits a warning rather than passing silently.

### What the receipt does not contain

The minted URL is a working administrator credential (single-use; the file 404s
after one visit). It is fetched, validated, and followed entirely inside the
browser, so it never reaches the CLI process, stdout, a log, or a receipt.
`credential_disclosed: false` is asserted by contract test, not by convention.
There is no flag to print it.

Failure states are distinguished on purpose:

| outcome | exit | meaning |
|---|---|---|
| `ok: true` | 0 | wp-admin readback confirmed a signed-in document |
| `refused` | 2 | ambiguous or unknown target; **nothing was minted** |
| `not_applicable` | 1 | the attempt failed cleanly |
| `unknown` | 3 | a login may exist. Do **not** repeat the command — check the browser first; an unused link expires on its own |

Reads retry once on a dropped browser bridge. `wp-login` never retries, because
a repeat mints a second credential rather than repeating a query.

A failed portal read names the condition OpenCLI reported, not a guess:
`portal_browser_not_connected` (Chrome is closed or the extension is off -- the
most common one, and it is *not* a login problem), `portal_read_timeout`,
`portal_adapter_unavailable`, `portal_account_identity_mismatch`, or
`portal_read_failed` when the adapter reported something not yet observed.
Act on the code, not on the prose.

If a login that used to work starts failing with `attach failed: Cannot access a
chrome-extension:// URL of different extension`, or hangs until a timer fires,
check for a second agent session or terminal on the browser bridge before
suspecting this tool -- the OpenCLI daemon and extension are a machine-wide
singleton:

```bash
ps aux | grep -c "[o]pencli --profile"   # 0 means the bridge is yours
```

Serialize rather than retrying; a retry during contention just mints another
unused credential. Details in the `opencli-usage` skill, **One bridge, one
session at a time**.

## Renewals

```bash
siteground-ops portal read <account-id> renewals
```

Reads the visible renewal cards. It never ticks, unticks, or submits anything.

**This account does not renew by default.** The operator lets plans reach expiry
and starts a new plan at Black Friday / Cyber Monday to take the new-plan
discount. So `selected_for_manual_renewal: false` on every row is the expected
steady state, not a lapse to flag, warn about, or offer to fix. Read it as a
confirmation that nothing is queued, and say so plainly.

`term`, `rate`, and `displayed_total` are **SiteGround's offer** if the service
were renewed at that term. They are not a scheduled charge and not a total due.
Nothing on this page is billed while every card is unticked.

A card is refused rather than reported blank when any visible field is missing,
or when the checkbox's `aria-checked` and its underlying input disagree. An
unreadable selection stays unreadable: reporting it as `false` would claim
"nothing renews" without having read it, which is the one claim this command
exists to make truthfully.

## Transport

- `doctor` and `inventory`: `auto` selects SSH when its local files and required
  credential names are ready; otherwise it selects the exact configured
  `novamira_server`. Use `--transport` for an explicit read path.
- An explicitly selected unavailable transport fails closed with exit `2`,
  `mutation_state: refused`, and `diagnostics.code: read_transport_unavailable`.
- Never fall back after SSH was selected. Host-key, authentication, connection,
  timeout, and remote-output failures remain SSH failures.
- Novamira reads call `novamira-ops/scripts/wp_ops.py` with constant read-only
  PHP and exact `--server`, but only after the SiteGround owner receipt verifies
  a pinned local `mcp-wordpress-remote@0.3.5` command and runtime-tree hash.
  This production path never uses the bridge's generic `@latest` fallback.
  It then requires returned `home_url()` parity before accepting evidence.
  Profiles store the server name, never its credentials.
- `sites` reports only locally selectable transports: bridge path, pinned
  runtime integrity, exact MCP server entry, SSH files, and required SSH names
  must all pass. A configured name by itself is not `ready`.
- Novamira cannot prove `wp sg`, so `siteground_cache_cli` is `null`. Its update
  lists come from cached WordPress transients and include separate core,
  plugin, and theme last-check times; require SSH inventory for a fresh remote
  update check. Inventory automation keys on transport-independent `id`, not
  the human display `name`.
- WordPress core/plugins/themes/database/filesystem/cache mutations: SSH + the explicit WP-CLI allowlist.
- Structured content/media: use WordPress REST/Application Passwords through the owning WordPress skill.
- SiteGround cache: `wp sg purge`, followed by an independent public GET/readback.
- WordPress admin sign-in: `wp-admin`, through the portal's own autologin API
  on the signed-in Chrome profile. The portal access token stays in that
  profile's `localStorage`; profiles store no token and no WordPress password.
- Site Tools account controls (provider backups, staging, DNS, SSL, PHP, email accounts): UI/OpenCLI fallback only until SiteGround publishes a stable API.
- SiteGround AI Agent is an optional interactive second operator. It is not an unattended API contract; impactful actions require its Power Mode.

## Mutation contract

Before a mutation, resolve the exact site, environment, recovery receipt, and request identity. Use:

```bash
siteground-ops cache-purge <site-id> \
  --confirm-target <site-id> \
  --recovery-receipt <provider-backup-or-project-receipt>
```

The site profile owns the environment; each CLI invocation generates the
request UUID. `--recovery-receipt` is a non-secret operator-supplied reference,
so verify that it points to an existing provider backup or project recovery
record before running the command. `applied` requires a successful `wp sg
purge`, matching WordPress `home_url` before and after, and a public GET that
returns HTTP 2xx/3xx without changing origin. A dropped connection after send
is `unknown`; independently read state before any retry.
Novamira is not a mutation fallback: `cache-purge` remains SSH/WP-CLI-only.

## Novamira update contract

Novamira CLI 1.0.2+ removed `--access read` and grants full access on login. Do not authorize a profile merely to make an E2E check pass. For the installed local package:

```bash
siteground-ops novamira-update check
siteground-ops novamira-update baseline --confirm-version 1.1.0
siteground-ops novamira-update apply --confirm-version 1.1.0
```

The updater refuses dirty package bytes, unknown package topology, missing npm signature/provenance verification, stale local guidance, or failed staged/offline checks. It installs the exact verified tarball bytes in an owned temporary Bun prefix with `--no-save`, commits a stable exact package version to the owner manifest/lock, runs candidate and post-apply checks with a scrubbed temporary HOME and network-denying sandbox, refreshes its owner baseline only after successful readback, serializes mutation with a process lock, and restores the exact package/dependency trees, modes, owner files, and baseline on failure. It preserves the local `novamira-ops` skill; profile inventory is read back before acceptance.

The LaunchAgent lane (`update-novamira-cli-stable.sh`) is reviewed-release-only:
it may auto-apply only the explicitly supported `1.1.0` candidate and otherwise
records a refusal. Install/inspect/remove it with `install.sh`,
`check-status.sh`, and `uninstall.sh`.

Exit `75` (EX_TEMPFAIL) from `novamira-update` means **no verdict**: the npm
registry was unreachable after three backed-off attempts, so nothing was
checked and nothing was changed. It is not a finding. The lane exits 0 on it and
increments `no_verdict_streak` in `status.json` -- read that number rather than
the last line of the log, because a single dropped DNS lookup and a week-long
outage produce the same single quiet run.

## Credentials and privacy

Site profiles store env/key paths, the exact non-secret `novamira_server`, and
credential pointers, never values. A Novamira-only profile uses
`adapter: "novamira_mcp"` and omits SSH paths. Do not print `.env`, Application
Passwords, SSH passphrases, cookies, private endpoints, or raw customer data. If
a provider capability is not publicly documented, label it unsupported/unknown
rather than reverse-engineering it into a production dependency.
