from __future__ import annotations

import unittest

from core.source_tree_bounds import apply_source_tree_bounds


class ArtifactSourceTreeBoundsTests(unittest.TestCase):
    def test_apply_source_tree_bounds_limits_depth_and_count(self) -> None:
        rows = [
            {"path": "a.py"},
            {"path": "pkg/b.py"},
            {"path": "pkg/sub/c.py"},
            {"path": "pkg/sub/deeper/d.py"},
        ]
        bounded = apply_source_tree_bounds(rows, max_files=2, max_depth=2)
        self.assertEqual([row["path"] for row in bounded], ["a.py", "pkg/b.py"])

    def test_apply_source_tree_bounds_no_limits_returns_all(self) -> None:
        rows = [{"path": "a.py"}, {"path": "b.py"}]
        bounded = apply_source_tree_bounds(rows, max_files=None, max_depth=None)
        self.assertEqual(bounded, rows)


if __name__ == "__main__":
    unittest.main()
