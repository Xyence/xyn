import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.repo_resolver import (
    RepoResolutionBlocked,
    RepoResolutionFailed,
    inspect_runtime_repo_map_targets,
    resolve_runtime_repo,
    runtime_repo_map,
    validate_runtime_repo_map_targets,
)


class RepoResolverTests(unittest.TestCase):
    def setUp(self):
        self.tempdirs = []

    def tearDown(self):
        os.environ.pop("XYN_RUNTIME_REPO_MAP", None)
        for tmpdir in self.tempdirs:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _temp_repo(self) -> Path:
        tmpdir = tempfile.mkdtemp(prefix="repo-resolver-")
        self.tempdirs.append(tmpdir)
        repo_path = Path(tmpdir)
        subprocess.run(["git", "init", "-b", "main"], cwd=tmpdir, check=True, capture_output=True, text=True)
        return repo_path

    def test_known_repo_resolves_correctly(self):
        repo = self._temp_repo()
        os.environ["XYN_RUNTIME_REPO_MAP"] = f'{{"xyn":["{repo}"]}}'
        resolved = resolve_runtime_repo("xyn")
        self.assertEqual(resolved.repo_key, "xyn")
        self.assertEqual(resolved.path, repo.resolve())

    def test_unknown_repo_fails_cleanly(self):
        os.environ["XYN_RUNTIME_REPO_MAP"] = "{}"
        with self.assertRaises(RepoResolutionFailed):
            resolve_runtime_repo("xyn-platform")

    def test_missing_unmounted_repo_fails_cleanly(self):
        os.environ["XYN_RUNTIME_REPO_MAP"] = '{"xyn":["/definitely/missing/path"]}'
        with self.assertRaises(RepoResolutionFailed):
            resolve_runtime_repo("xyn")

    def test_non_git_path_fails_cleanly(self):
        tmpdir = tempfile.mkdtemp(prefix="repo-resolver-nongit-")
        self.tempdirs.append(tmpdir)
        os.environ["XYN_RUNTIME_REPO_MAP"] = f'{{"xyn":["{tmpdir}"]}}'
        with self.assertRaises(RepoResolutionFailed):
            resolve_runtime_repo("xyn")

    def test_ambiguous_mapping_blocks(self):
        repo1 = self._temp_repo()
        repo2 = self._temp_repo()
        os.environ["XYN_RUNTIME_REPO_MAP"] = f'{{"xyn":["{repo1}","{repo2}"]}}'
        with self.assertRaises(RepoResolutionBlocked):
            resolve_runtime_repo("xyn")

    def test_absolute_path_override_resolves(self):
        repo = self._temp_repo()
        resolved = resolve_runtime_repo(str(repo))
        self.assertEqual(resolved.path, repo.resolve())

    def test_validate_runtime_repo_map_targets_reports_missing_candidates(self):
        os.environ["XYN_RUNTIME_REPO_MAP"] = '{"xyn":["/definitely/missing/path"],"xyn-platform":["/also/missing"]}'
        warnings = validate_runtime_repo_map_targets()
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("repo 'xyn'" in row for row in warnings))
        self.assertTrue(any("repo 'xyn-platform'" in row for row in warnings))

    def test_validate_runtime_repo_map_targets_accepts_existing_git_candidate(self):
        repo = self._temp_repo()
        os.environ["XYN_RUNTIME_REPO_MAP"] = f'{{"xyn":["/definitely/missing/path","{repo}"]}}'
        warnings = validate_runtime_repo_map_targets()
        self.assertEqual(warnings, [])

    def test_validate_runtime_repo_map_targets_reports_unreadable_candidate(self):
        repo = self._temp_repo()
        os.environ["XYN_RUNTIME_REPO_MAP"] = f'{{"xyn":["{repo}"]}}'
        with mock.patch("core.repo_resolver.os.access", return_value=False):
            warnings = validate_runtime_repo_map_targets()
        self.assertEqual(len(warnings), 1)
        self.assertIn("not_readable", warnings[0])

    def test_runtime_repo_map_supports_colon_pair_syntax(self):
        repo = self._temp_repo()
        os.environ["XYN_RUNTIME_REPO_MAP"] = f"xyn:{repo}"
        parsed = runtime_repo_map()
        self.assertIn("xyn", parsed)
        self.assertEqual(parsed["xyn"][0], repo.resolve())

    def test_inspect_runtime_repo_map_targets_reports_empty_directory(self):
        tmpdir = tempfile.mkdtemp(prefix="repo-resolver-empty-")
        self.tempdirs.append(tmpdir)
        os.environ["XYN_RUNTIME_REPO_MAP"] = f"xyn-platform:{tmpdir}"
        rows = inspect_runtime_repo_map_targets()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["exists"])
        self.assertTrue(rows[0]["is_dir"])
        self.assertTrue(rows[0]["is_empty"])
        self.assertFalse(rows[0]["valid"])


if __name__ == "__main__":
    unittest.main()
