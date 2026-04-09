#!/usr/bin/env python3
"""Inspect runtime repo-map targets without changing runtime behavior."""

from __future__ import annotations

import json
import sys

from core.repo_resolver import RepoResolutionBlocked, inspect_runtime_repo_map_targets, validate_runtime_repo_map_targets


def main() -> int:
    try:
        rows = inspect_runtime_repo_map_targets()
    except RepoResolutionBlocked as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "repo_map_invalid",
                    "message": str(exc),
                    "targets": [],
                    "warnings": [f"Runtime repo map configuration is invalid: {exc}"],
                },
                indent=2,
            )
        )
        return 2

    warnings = validate_runtime_repo_map_targets()
    print(
        json.dumps(
            {
                "ok": True,
                "targets": rows,
                "warnings": warnings,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
