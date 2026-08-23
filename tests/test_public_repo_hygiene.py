"""This repository is public. Every hostname in it must be a placeholder.

Why a test and not a paragraph in AGENTS.md: a fixture naming a real customer
site looks exactly like a fixture naming a fake one, nothing fails when someone
adds it, and a public commit cannot be taken back. Silent, repeating, and
decidable -- so it is a gate rather than a sentence.

Deliberately the test does NOT list the real domains it is defending against.
Spelling them here would republish the very strings the sanitization removed,
and would only ever catch today's list. It inverts the question instead: every
registrable domain found in the tree must appear in ALLOWED. A new real domain
fails by default, which is the failure direction that is safe to be wrong in.

Scope: hostnames only. Nothing here grades whether a value is secret -- see the
credential guidance in AGENTS.md.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]

# Registrable domains this project is allowed to name: the provider's own
# surfaces, ecosystem hosts, and the example-* placeholders the fixtures use.
ALLOWED = frozenset({
    "siteground.com",
    "sg-host.com",
    "sitegroundcdn.com",
    "example.com",
    "example.org",
    "example.net",
    "example-main.com",
    "example-shop.com",
    "example-studio.com",
    "example-lang.com",
    "example-growth.com",
    "github.com",
    "githubusercontent.com",
    "wordpress.org",
    "python.org",
    "nodejs.org",
    "npmjs.com",
    "anthropic.com",
    "schema.org",
    "apple.com",
    "npmjs.org",
    "example-other.com",
    "example-client.co.uk",
    "w3.org",
    # Carried in by the verbatim AGPL-3.0 text in LICENSE and the notice in
    # README. Allowlisted rather than excluding those files: carving out a
    # path is how a gate quietly stops grading the tree it names.
    "gnu.org",
    "fsf.org",
    "shields.io",  # README license badge
})

# A bare dotted-token regex is unusable here: `args.target`, `array.from` and
# `novamira-3.tgz` all have the shape of a hostname. Anchoring on a real public
# suffix is what separates a host from an attribute access, and it is why this
# gate reports the count it graded -- an extractor that quietly stops matching
# is the failure mode a green test cannot show you.
PUBLIC_SUFFIX = "com|net|org|io|dev|co|ai|cloud|xyz|uk|au|jp|de|fr|nl|ca"
# Two-label public suffixes, so `hi.example-client.co.uk` registers as
# `example-client.co.uk` rather than as `co.uk`.
COMPOUND_SUFFIX = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "co.jp", "co.nz", "com.br", "com.cn", "com.tw", "co.kr",
})
HOSTNAME = re.compile(
    r"(?:(?<=://)|(?<=[\s'\"`(,=@]))"                       # URL scheme, or a value boundary
    r"((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,4}(?:" + PUBLIC_SUFFIX + r"))\b",
    re.IGNORECASE,
)


def committable_files() -> list[Path]:
    """What could reach the public remote -- tracked files plus new ones.

    `git ls-files` alone lists tracked files only, and a fresh leak arrives with
    the new file that needed it. --exclude-standard keeps .gitignore honoured.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    return [REPO / name for name in out if (REPO / name).is_file()]


def registrable(host: str) -> str:
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in COMPOUND_SUFFIX:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def test_every_hostname_in_the_public_tree_is_a_placeholder() -> None:
    files = committable_files()
    assert files, "found no committable files: the selector is broken, not the tree clean"

    offenders: dict[str, set[str]] = {}
    graded = 0
    for path in files:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for match in HOSTNAME.findall(text):
            # A bare public suffix is not a host: it needs a label in front of
            # it. Without this the gate flags its own COMPOUND_SUFFIX table.
            if match.lower() in COMPOUND_SUFFIX:
                continue
            graded += 1
            domain = registrable(match)
            if domain not in ALLOWED:
                offenders.setdefault(domain, set()).add(str(path.relative_to(REPO)))

    # Printed so a selector that silently narrows later shows up as a number
    # that dropped, rather than as continued green.
    print(f"graded {graded} hostnames across {len(files)} committable files")
    assert graded > 50, "hostname extraction collapsed; the gate is grading almost nothing"
    assert not offenders, (
        "non-placeholder domain(s) would be published: "
        + "; ".join(f"{d} in {sorted(f)}" for d, f in sorted(offenders.items()))
        + ". Replace with an example-* placeholder, or add it to ALLOWED if it is "
        "genuinely a public third-party host."
    )


@pytest.mark.parametrize("prefix", ["", "staging2."])
def test_the_gate_can_actually_fail(prefix: str) -> None:
    """A gate that has only ever passed is not evidence.

    Proves the predicate rejects a real-looking hostname, without writing one
    into the repository to do it.
    """
    leak = prefix + "acme-" + "customer-" + "site" + "." + "com"
    matches = HOSTNAME.findall(f"admin_url = 'https://{leak}/wp-admin'")
    assert matches, "the extractor missed a hostname it must catch"
    assert all(registrable(m) not in ALLOWED for m in matches)
