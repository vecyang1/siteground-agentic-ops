#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from siteground_ops.portal import register_opencli_adapter  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Idempotently register the repository-owned read-only SiteGround OpenCLI adapter."
    )
    parser.add_argument(
        "--opencli-path",
        type=Path,
        default=Path.home() / ".local" / "bin" / "opencli",
    )
    parser.add_argument(
        "--adapter-dir",
        type=Path,
        default=ROOT / "opencli" / "siteground",
    )
    parser.add_argument("--home", type=Path, default=None)
    args = parser.parse_args(argv)

    status = register_opencli_adapter(
        args.opencli_path,
        args.adapter_dir,
        home=args.home,
    )
    print(json.dumps(status, sort_keys=True))
    return 0 if status["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
