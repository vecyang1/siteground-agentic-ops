# SiteGround Agentic Ops Agent Instructions

This repository owns the deterministic SiteGround/WordPress operations control plane and the Novamira CLI update lane.

- Reads are the default. Every mutation needs an explicit target, confirmation, recovery pointer, and independent readback.
- Never expose arbitrary shell or arbitrary WP-CLI execution through the public CLI.
- Site profiles contain only non-secret configuration and pointers. Secrets stay in approved project env files or 1Password.
- SSH host-key verification is mandatory. Unknown or changed hosts fail closed.
- Novamira updates must preserve the local `novamira-ops` skill. Never run a vendor skill installer over it.
- Follow RED -> GREEN TDD for behavior changes. Run the full local suite before release.
- The suite is two commands, and the JS half needs the glob. `node --test
  opencli/siteground/` resolves the directory as a module and dies with
  `MODULE_NOT_FOUND`, which reads as a broken adapter rather than a wrong
  invocation -- an equivalent you composed is not the check.
  ```bash
  PYTHONPATH=src python3 -m pytest -q
  node --test "opencli/siteground/*.test.mjs"
  ```
- Editing any `opencli/siteground/*.js` invalidates `PORTAL_PLUGIN_SOURCE_SHA256`
  in `src/siteground_ops/portal.py`, and registration then reports
  `source_invalid`. That is the pin working. Re-pin from the files rather than
  transcribing digests, and read the count it reports -- exactly the files you
  edited should drift.
  ```bash
  python3 -c 'import hashlib,pathlib
  for p in sorted(pathlib.Path("opencli/siteground").glob("*.js")):
      print(f"    \"{p.name}\": \"{hashlib.sha256(p.read_bytes()).hexdigest()}\",")'
  ```
- Generated status/log/runtime files stay ignored. Keep disk use bounded and avoid duplicate package archives.
- **This repository is public.** Every hostname in it is a placeholder (`example-*`); real customer domains belong nowhere, including fixtures, docs, and changelog entries. `tests/test_public_repo_hygiene.py` fails on any domain outside its allowlist and names the offending file -- it is the gate, this bullet is only the signpost.
- The live end-to-end check mints a real single-use administrator credential on a live account, so it is opt-in and never runs in the normal suite: `SITEGROUND_E2E=1 scripts/e2e-wp-admin.py --target <domain> --app <id>`. Run it after touching the login path, the OpenCLI timeout budgets, or the adapter plugin.
- Licensed AGPL-3.0-or-later, not MIT and not plain GPL: the way an ops tool
  gets taken proprietary is a hosted dashboard running a modified copy, which
  distributes nothing and so never triggers GPL-3.0. Contributions are accepted
  on those terms. Keep new dependencies compatible -- permissive (MIT, BSD,
  Apache-2.0) and LGPL are fine; anything GPL-2.0-only is not.
