import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from javamod.report import RunReport


def make_report(**overrides):
    defaults = dict(
        source="https://github.com/acme/app.git", source_ref=None, build_tool="maven", build_root=".",
        target_java=21, boot_target=None, profile="standard", engine="openrewrite", recipes=["x"],
        changed=True, diff_stat="1 file changed", build_ok=True, build_output_tail="", commit="abc123",
        branch="modernize-java21", destination=None, pushed=False,
    )
    defaults.update(overrides)
    return RunReport(**defaults)


class WriteJsonDestinationTests(unittest.TestCase):
    def test_dash_writes_to_stdout_not_a_file(self):
        report = make_report()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            report.write_json("-")
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["commit"], "abc123")

    def test_path_writes_to_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "report.json"
            make_report().write_json(str(path))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["branch"], "modernize-java21")


if __name__ == "__main__":
    unittest.main()
