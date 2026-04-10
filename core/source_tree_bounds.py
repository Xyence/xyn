from __future__ import annotations

from typing import Any, Optional


def apply_source_tree_bounds(
    rows: list[dict[str, Any]],
    *,
    max_files: Optional[int],
    max_depth: Optional[int],
) -> list[dict[str, Any]]:
    bounded = list(rows or [])
    if max_depth is not None:
        depth_limit = max(1, int(max_depth))
        bounded = [
            row
            for row in bounded
            if isinstance(row, dict) and len(str(row.get("path") or "").split("/")) <= depth_limit
        ]
    if max_files is not None:
        bounded = bounded[: max(1, int(max_files))]
    return bounded
