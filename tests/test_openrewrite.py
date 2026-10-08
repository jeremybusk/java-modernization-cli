import tempfile
import unittest
from pathlib import Path
from unittest import mock

from javamod import openrewrite, recipes
from javamod.discover import BuildRoot


class ScratchDirectoryTests(unittest.TestCase):
    def test_recipe_run_preserves_existing_javamod_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / ".javamod-openrewrite"
            existing.mkdir()
            note = existing / "keep.txt"
            note.write_text("user data")
            build = BuildRoot(root, "maven")
            plan = recipes.build_plan(build, target_java=21, boot=None, profile="conservative")
            with mock.patch("javamod.openrewrite._run"), mock.patch("javamod.openrewrite._has_changes", return_value=False):
                openrewrite.run(build, plan, recipe_source="maven-central", allow_codegenome=False, log=lambda _: None)
            self.assertEqual(note.read_text(), "user data")
            self.assertEqual(list(root.glob(".javamod-openrewrite-*")), [])


if __name__ == "__main__":
    unittest.main()
