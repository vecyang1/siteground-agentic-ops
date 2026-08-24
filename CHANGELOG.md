# Changelog

## Unreleased

- Fix the candidate sandbox, which could never execute the CLI it was built to
  grade. `profile_inventory` and `offline_doctor` run the entry point under
  `sandbox-exec`, and the profile allowed `process-exec` on Bun only, while the
  entry point is a symlink into `node_modules`. Every `apply` would have refused
  with `profile_inventory_failed`; nothing caught it because the fakes stand in
  for the sandbox and the version pin meant `apply` had never once run for real.
  Three rules were missing: exec on the path the CLI is *called* by as well as
  the one it resolves to, `file-read-metadata` on each directory between HOME
  and an allowed path (Node realpaths its main module and lstats every
  component, which EPERMs under a blanket HOME deny), and read access to the
  package's own declared dependencies. Content denies, the sensitive-path
  denies, and `(deny network*)` are unchanged.
- Review and adopt `@novamira/cli` 1.1.0 (published 2026-08-11); the reviewed
  pin moves from 1.0.3. Evidence: npm SRI and signature/provenance both verify;
  no new dependencies (`commander ^14` only), no install scripts, `node >=22`
  unchanged; the only network host in `dist/` is still the npm registry; the
  required Novamira server version is unchanged at 1.11.1. It adds an OAuth
  device grant, and profiles gain an *optional* `clientGrant` field that older
  profiles are explicitly documented to omit -- so adopting it migrates nothing.
  The one-way note: a profile written by 1.1.0 carries a key 1.0.3's validator
  rejects, so a downgrade after a profile write would need the profile rewritten.
- Grade the installed package against the versions review has cleared, not
  against the registry's latest. Once upstream shipped 1.1.0 the check reported
  `installed_version_requires_review:1.0.3` about a package that had been
  reviewed and verified -- a blocker that named the wrong thing and could only
  appear when a second, real blocker was already firing.
- Treat an unreachable npm registry as *no verdict*, not as a failed check. Five
  of fifteen daily runs had failed on the resolver rather than on the package
  (`urlopen error [Errno 8]`, and a 120s npm timeout), and the lane reported
  both as `check failed`. The registry read now retries a transport failure
  three times with backoff -- never an HTTP status, which is an answer -- and an
  exhausted retry exits `75` (EX_TEMPFAIL) with `novamira_registry_unreachable`.
  The lane exits 0 on it and records `no_verdict_streak`, so a dropped lookup
  stays quiet while a real outage becomes a number that climbs.
- `_verify_npm_signatures` raises on a timeout instead of returning `False`. The
  same `False` means "this release has no provenance", and a slow link is not
  evidence about a signature.
- Carry OpenCLI's own failure condition into the portal receipt. Every portal
  failure reported `portal_read_failed` with a remedy naming the SiteGround
  login, while the live failure was a disconnected browser bridge -- a correct
  remedy sitting one line below a wrong diagnosis. `PortalError` now carries the
  condition, and only conditions with an observed adapter sample are mapped.
- Close the last gap between the portal inventory and the local profiles: the
  one remaining unprofiled site is now registered as `portal_only`, so its
  Site Tools links and `wp-admin` work while it has no read transport. It has
  none because it answers 404 on `/wp-json/mcp/novamira` and sits on a different
  hosting account from the one SSH-enabled profile -- measured by listing that
  account's document roots, not assumed from the shared-hosting layout, which
  turns out to expose exactly one site plus its staging copy per account.
  Giving it a read transport needs the Novamira MCP plugin installed or its own
  SSH credential; both are provider-side writes and neither is this tool's call.
- `belovedpals-siteground` moves from `portal_only` to `novamira_mcp`. Its MCP
  server and credential already existed and the site answers
  `/wp-json/mcp/novamira`; the profile was simply stale, so the site had been
  listed as unreadable while `doctor` and `inventory` both work against it.

- Relicense from MIT to AGPL-3.0-or-later. The realistic way an ops tool gets
  taken proprietary is a hosted dashboard running a modified copy, which
  distributes nothing and so never triggers plain GPL; AGPL section 13 covers
  it. Dependencies stay compatible and unvendored -- OpenCLI is Apache-2.0,
  `paramiko` is LGPL-2.1-or-later, both one-way compatible into AGPL-3.0.
  Commits already published under MIT remain MIT for anyone who fetched them;
  the change binds from here forward.
- `gnu.org` and `fsf.org` join the public-repo hostname allowlist, because the
  license text names them. Allowlisting beats excluding LICENSE from the scan:
  a carve-out is how a gate stops grading the tree it claims to cover.
- Add `siteground-ops wp-admin <site-id-or-domain>`: signs into one exact
  WordPress application through SiteGround's own single-use autologin API
  (`/v1/auth/wordpress/autologin/<site-id>/<app-id>`), the mechanism behind the
  portal's "WordPress Admin" button. No WordPress password is involved or stored.
- Add read command `opencli siteground wp-apps` and the plugin's first write
  command `opencli siteground wp-login`. Access is now declared per command and
  the readiness gate checks it, so a command that drifts from read to write
  cannot keep passing.
- The minted autologin URL is a working administrator credential. It is fetched,
  validated for host/scheme/shape, and consumed inside the browser, so it never
  reaches the CLI process, stdout, or a receipt. A contract test asserts no
  command declares it as an output column, and there is no flag to print it.
- Follow the login in a new tab rather than navigating the bound portal tab.
  The adapter session is bound to `siteground.com`, so navigating it to the
  customer domain detached the target mid-flight.
- Refuse ambiguity instead of guessing: the WordPress application id is read
  from the portal (it is not always `1`), a site with a staging copy is refused
  until `--app` names one, and `admin_host` is tracked separately because every
  application reports the *site's* domain.
- Distinguish outcomes: `refused` (nothing minted, exit 2), clean failure
  (exit 1), and `unknown` (exit 3, do not repeat — a login may exist). Reads
  retry once on a dropped browser bridge; `wp-login` never retries.
- The login tab opens in the background by default; `--foreground` raises it.
- Surface OpenCLI's own refusal text instead of a generic non-zero status, by
  reading both its JSON and YAML error envelopes.
- Fix a stale `PORTAL_PLUGIN_SOURCE_SHA256` pin for `_ui.js` and `renewals.js`
  that had been reporting the registered plugin as `source_invalid` and was
  failing six registration tests.
- Fix a stale `_ui.test.mjs` selector expectation.
- Fix `renewals`, which had been reporting every card with an empty term, rate,
  and total. The cause was the card boundary, not the labels: the checkbox's
  nearest matching ancestor spans only the identity column, while SiteGround
  renders the offered term and the displayed total in two sibling columns of the
  same row. The page now returns the three column texts verbatim and `_ui.js`
  parses them, so the parsing is covered by tests against captured page text
  instead of living in an evaluate() string no test can run.
- Restore the renewal completeness guard on top of that fix, scoped so it cannot
  invert the account's posture: an unticked card is a complete, valid reading,
  never an incomplete one. This account deliberately renews nothing and starts a
  new plan at BFCM for the discount, so an all-`false` page is the expected
  steady state. Only a genuinely unreadable card is refused, and the refusal
  names the missing fields and states that it is a read failure, not a renewal
  setting.
- Refuse an unreadable selection instead of reporting it as `false`. If a card's
  `aria-checked` and its underlying input disagree, the row is refused, because
  reporting `false` would claim "nothing renews" without having read it.
- Give `wp-login` a browser budget that fits a real login. OpenCLI caps a browser
  command at 60s by default, under the 30-40s a healthy login takes, so the
  command aborted mid-flight on a slower site and reported `wordpress_login_failed`.
  The adapter command exposes no `--timeout` flag -- OpenCLI's own timeout hint
  names one that does not parse -- so the budget is set through
  `OPENCLI_BROWSER_COMMAND_TIMEOUT`, and the subprocess budget is derived from it
  plus the browser connect allowance so the two cannot race.
- Treat OpenCLI's own timeout on a write as `unknown`, not a clean failure.
  It exits non-zero like any refusal, so it was reported as
  `wordpress_login_failed` / `not_applicable` with a `safe_next_action` that
  invited a retry -- and a retry mints a second single-use administrator
  credential. A timeout is now detected from the error envelope's `TIMEOUT` code
  and surfaces as `mutation_state: unknown`, exit 3, "do not repeat".
- Extend the timeout-budget invariant to every portal command, not just
  `wp-login`. The wrapper's default 45s sat *below* OpenCLI's 60s browser cap,
  so for the eight ordinary reads the wrapper always fired first and an
  attributable timeout arrived as an opaque "did not complete". The subprocess
  budget is now derived from the browser budget inside `_run`, so the two cannot
  be set independently, and the test grades all nine commands.
- Add `scripts/e2e-wp-admin.py`: a gated live check that signs in for real and
  measures the login against the configured budget. That margin is the thing no
  offline test can see, and its erosion is what caused the abort above.
- Add `tests/test_public_repo_hygiene.py`. This repository is public, so a
  fixture naming a real customer site is a permanent disclosure that looks
  exactly like a fixture naming a fake one. The gate inverts the question --
  every hostname must be in an allowlist -- rather than listing the real domains
  it defends against, which would republish them and would only catch today's
  list. It found one real domain that a name-by-name scan had missed.

## 0.2.0 - 2026-08-09

- Added exact-site Novamira MCP fallback for read-only `doctor` and `inventory`, including Novamira-only profiles when SSH keys are unavailable.
- Added explicit `--transport auto|ssh|novamira`, visible transport receipts, fixed read-only PHP programs, bounded bridge execution, and mandatory `home_url()` parity.
- Pinned the Novamira bridge to a locally verified `mcp-wordpress-remote@0.3.5` runtime receipt instead of its generic `@latest` fallback; runtime, bridge, and exact-server readiness now fail closed.
- Enforced stdout/stderr limits while streaming, terminated the bridge process group on overflow, and converted malformed SSH owner files into structured fallback readiness.
- Normalized inventory with stable plugin/theme `id` fields and separate core/plugin/theme cached-update timestamps; left unproven WP-CLI cache availability as `null` instead of false.
- Preflighted explicit SSH selection and stopped Novamira-primary profiles from advertising optional SSH fields as a usable transport.
- Kept cache purge and every mutation SSH/WP-CLI-only; runtime SSH failures never trigger hidden cross-transport retries.

## 0.1.1 - 2026-08-09

- Refreshed the Novamira owner baseline only after successful post-update readback and included it in rollback verification, preventing the updater from flagging its own manifest/lock changes as user edits.
- Missing owner baselines now fail closed; a one-time explicit baseline command is required before unattended updates on a new machine.

## 0.1.0 - 2026-08-09

- Added the configuration-driven `siteground-ops` CLI for strict SiteGround SSH/WP-CLI diagnostics and guarded cache operations.
- Added JSON receipts, explicit target/recovery gates, redacted diagnostics, and fail-closed unknown mutation handling.
- Added provenance-aware Novamira CLI checks and updates with staged offline doctor validation, exact Bun command enforcement, profile preservation, and owner-file rollback readback.
- Added runtime symlink discovery and a symlink-safe launcher without storing credential values.
- Hardened Novamira updates with npm signature/provenance verification, pinned artifact installation, exact package-tree rollback, topology validation, fail-closed doctor parsing, scrubbed sandboxed candidate checks, and a process lock.
- Added the reviewed-release-only LaunchAgent lane (`com.vec.siteground-novamira-cli-updater`) with JSON status receipts; local `novamira-ops` edits remain untouched.
- Hardened SiteGround readback and receipts: HTTPS-only public URLs, target-bound cache identity checks, dot-path rejection, explicit update-check failures, and key-aware secret redaction.
