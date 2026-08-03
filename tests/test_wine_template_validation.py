from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "docker" / "validate_wine_template.py"
SPEC = importlib.util.spec_from_file_location("validate_wine_template", MODULE_PATH)
validate_wine_template = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = validate_wine_template
SPEC.loader.exec_module(validate_wine_template)


class WineTemplateTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name).resolve()
        self.template = self.root / "template"
        self.app_home = self.root / "app-home"
        self.app_home.mkdir(parents=True)
        self.app_home.chmod(0o700)

        user = self.template / "drive_c" / "users" / "nef-watch"
        dosdevices = self.template / "dosdevices"
        user.mkdir(parents=True)
        dosdevices.mkdir()
        (self.template / "sealed.txt").write_text("sealed\n", encoding="utf-8")
        (user / "Desktop").symlink_to(self.app_home)
        templates = user / "AppData" / "Roaming" / "Microsoft" / "Windows"
        templates.mkdir(parents=True)
        (templates / "Templates").symlink_to(self.app_home)
        (dosdevices / "c:").symlink_to("../drive_c")
        (self.template / "sealed.txt").chmod(0o444)
        for directory, _subdirs, _files in os.walk(self.template, topdown=False):
            Path(directory).chmod(0o555)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def validate(self) -> None:
        validate_wine_template.validate_template_tree(
            self.template,
            self.app_home,
            expected_template_owner=os.getuid(),
        )

    def test_allowed_desktop_link_to_writable_app_home_is_not_a_failure(self) -> None:
        self.validate()

    def test_unexpected_symlink_or_writable_persistent_file_fails(self) -> None:
        unexpected = self.template / "drive_c" / "escape"
        unexpected.parent.chmod(0o755)
        unexpected.symlink_to(self.app_home)
        unexpected.parent.chmod(0o555)
        with self.assertRaises(validate_wine_template.TemplateValidationError):
            self.validate()
        unexpected.parent.chmod(0o755)
        unexpected.unlink()
        unexpected.parent.chmod(0o555)

        sealed = self.template / "sealed.txt"
        sealed.chmod(0o644)
        with self.assertRaises(validate_wine_template.TemplateValidationError):
            self.validate()

    def test_template_owner_is_part_of_the_seal(self) -> None:
        with self.assertRaisesRegex(
            validate_wine_template.TemplateValidationError, "owner-sealed"
        ):
            validate_wine_template.validate_template_tree(
                self.template,
                self.app_home,
                expected_template_owner=os.getuid() + 1,
            )


if __name__ == "__main__":
    unittest.main()
