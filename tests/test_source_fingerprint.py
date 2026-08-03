import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "docker" / "source_fingerprint.py"
SPEC = importlib.util.spec_from_file_location("source_fingerprint", MODULE_PATH)
source_fingerprint = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = source_fingerprint
SPEC.loader.exec_module(source_fingerprint)


class SourceFingerprintTests(unittest.TestCase):
    def test_checked_out_tree_matches_tracked_git_commit(self):
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            checkout.mkdir()
            shutil.copytree(ROOT / "docker", checkout / "docker")
            for relative in source_fingerprint.FIXED_FILES:
                source = ROOT / relative
                target = checkout / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
            subprocess.run(
                ["git", "-C", str(checkout), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(checkout), "config", "user.name", "test"], check=True
            )
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(checkout), "commit", "--quiet", "-m", "test"], check=True
            )
            commit = subprocess.check_output(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
            ).strip()
            tree_hash = source_fingerprint.fingerprint_tree(checkout)
            git_hash = source_fingerprint.fingerprint_git_commit(checkout, commit)
            original_limit = source_fingerprint.MAX_GIT_TREE_ENTRIES
            try:
                source_fingerprint.MAX_GIT_TREE_ENTRIES = 1
                with self.assertRaisesRegex(
                    source_fingerprint.FingerprintError, "too many"
                ):
                    source_fingerprint.fingerprint_git_commit(checkout, commit)
            finally:
                source_fingerprint.MAX_GIT_TREE_ENTRIES = original_limit
        self.assertEqual(tree_hash, git_hash)

    def test_tree_rejects_symlinked_fixed_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docker").mkdir()
            for relative in source_fingerprint.FIXED_FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative, encoding="utf-8")
            target = root / "real-requirements.txt"
            target.write_text("safe", encoding="utf-8")
            (root / "requirements.txt").unlink()
            (root / "requirements.txt").symlink_to(target)
            with self.assertRaisesRegex(source_fingerprint.FingerprintError, "regular file"):
                source_fingerprint.fingerprint_tree(root)

    def test_git_commit_rejects_candidate_symlink(self):
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "test"], check=True
            )
            (repo / "docker").mkdir()
            for relative in source_fingerprint.FIXED_FILES:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative, encoding="utf-8")
            target = repo / "target"
            target.write_text("secret", encoding="utf-8")
            (repo / "requirements.txt").unlink()
            (repo / "requirements.txt").symlink_to("target")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "test"], check=True)
            commit = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            with self.assertRaisesRegex(source_fingerprint.FingerprintError, "regular Git blob"):
                source_fingerprint.fingerprint_git_commit(repo, commit)


if __name__ == "__main__":
    unittest.main()
