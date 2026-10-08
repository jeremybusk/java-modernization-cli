import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from javamod import ai
from javamod.discover import BuildRoot


class FakeClient:
    def __init__(self, text):
        self.text = text
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **_kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


class AskForFilesTests(unittest.TestCase):
    def test_parses_plain_json(self):
        client = FakeClient('[{"path": "A.java", "content": "class A {}"}]')
        result = ai._ask_for_files(client, "model", "system", "user")
        self.assertEqual(result, {"A.java": "class A {}"})

    def test_strips_markdown_fences(self):
        client = FakeClient('```json\n[{"path": "A.java", "content": "class A {}"}]\n```')
        result = ai._ask_for_files(client, "model", "system", "user")
        self.assertEqual(result, {"A.java": "class A {}"})

    def test_ignores_entries_without_a_path(self):
        client = FakeClient('[{"content": "oops"}, {"path": "B.java", "content": "class B {}"}]')
        result = ai._ask_for_files(client, "model", "system", "user")
        self.assertEqual(result, {"B.java": "class B {}"})


class ImplicatedFilesTests(unittest.TestCase):
    def _build(self, tmp: Path, tool: str) -> BuildRoot:
        source = tmp / "src/main/java/com/example/App.java"
        source.parent.mkdir(parents=True)
        source.write_text("class App {}", encoding="utf-8")
        return BuildRoot(path=tmp, tool=tool)

    def test_maven_error_lines_with_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = self._build(Path(tmp), "maven")
            output = (f"[ERROR] {tmp}/src/main/java/com/example/App.java:[42,7] cannot find symbol\n"
                      f"[ERROR] {tmp}/src/main/java/com/example/App.java:[50,1] cannot find symbol\n"
                      "[ERROR] /elsewhere/Other.java:[1,1] outside the build\n")
            self.assertEqual(ai.implicated_files(build, output), ["src/main/java/com/example/App.java"])

    def test_gradle_error_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = self._build(Path(tmp), "gradle")
            output = f"{tmp}/src/main/java/com/example/App.java:42: error: cannot find symbol\n"
            self.assertEqual(ai.implicated_files(build, output), ["src/main/java/com/example/App.java"])

    def test_failing_tests_are_not_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = self._build(Path(tmp), "maven")
            output = "[ERROR] Failures:\n[ERROR]   AppTest.works:3 expected: <1> but was: <2>\n[INFO]\n"
            self.assertEqual(ai.implicated_files(build, output), [])


if __name__ == "__main__":
    unittest.main()
