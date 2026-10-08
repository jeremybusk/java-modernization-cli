import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from javamod import ai
from javamod.buildcheck import BuildResult
from javamod.discover import BuildRoot
from javamod.errors import ModError


class FakeClient:
    def __init__(self, text):
        self.text = text
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **_kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


class AskForFilesTests(unittest.TestCase):
    def test_invalid_response_shapes_raise_user_facing_errors(self):
        for response in ('{}', '[42]', '[{"path": 42, "content": "x"}]', '[{"path": "A.java"}]'):
            with self.subTest(response=response), self.assertRaises(ModError):
                ai._ask_for_files(FakeClient(response), "model", "system", "user")

    def test_truncated_response_is_rejected_even_if_json_is_valid(self):
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
            stop_reason="max_tokens", content=[SimpleNamespace(type="text", text="[]")])))
        with self.assertRaisesRegex(ModError, "truncated"):
            ai._ask_for_files(client, "model", "system", "user")

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


class RewriteRegressionTests(unittest.TestCase):
    def test_unactionable_build_does_not_require_an_ai_client(self):
        failed = BuildResult(False, [], "mvn is not installed")
        with mock.patch("javamod.ai._client") as client:
            result = ai.fix_build(BuildRoot(Path("."), "maven"), failed, model="model",
                                  max_iterations=1, run_tests=True, log=lambda _: None)
        self.assertIs(result, failed)
        client.assert_not_called()

    def test_build_fixes_can_only_edit_supplied_java_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.java").write_text("class App {}")
            config = root / "pom.xml"
            config.write_text("<project/>")
            build = BuildRoot(root, "maven")
            failed = BuildResult(False, [], f"[ERROR] {root}/App.java:[1,1] cannot find symbol")
            with mock.patch("javamod.ai._client", return_value=FakeClient(
                    '[{"path":"pom.xml","content":"overwrite"},{"path":"App.java","content":"class App {int x;}"}]')), \
                    mock.patch("javamod.buildcheck.validate", return_value=BuildResult(True, [], "")):
                result = ai.fix_build(build, failed, model="model", max_iterations=1, run_tests=True, log=lambda _: None)
            self.assertTrue(result.ok)
            self.assertEqual(config.read_text(), "<project/>")
            self.assertEqual((root / "App.java").read_text(), "class App {int x;}")

    def test_modernization_covers_modules_in_stable_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pom.xml").write_text("<project><modules><module>app</module></modules></project>")
            source = root / "app/src/main/java"
            source.mkdir(parents=True)
            (root / "app/pom.xml").write_text("<project/>")
            for name in ("B.java", "A.java"):
                (source / name).write_text("class Original {}")
            with mock.patch("javamod.ai._client", return_value=FakeClient("class Modern {}")):
                changed = ai.modernize_tree(BuildRoot(root, "maven"), target_java=21,
                                           model="model", max_files=1, log=lambda _: None)
            self.assertEqual(changed, ["app/src/main/java/A.java"])
            self.assertEqual((source / "B.java").read_text(), "class Original {}")

    def test_truncated_source_response_leaves_original_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pom.xml").write_text("<project/>")
            source = root / "src/main/java/A.java"
            source.parent.mkdir(parents=True)
            source.write_text("class Original {}")
            client = mock.Mock()
            client.messages.create.return_value = SimpleNamespace(
                stop_reason="max_tokens", content=[SimpleNamespace(type="text", text="class CutOff {")])
            with mock.patch("javamod.ai._client", return_value=client), self.assertRaisesRegex(ModError, "truncated"):
                ai.modernize_tree(BuildRoot(root, "maven"), target_java=21, model="model", max_files=1)
            self.assertEqual(source.read_text(), "class Original {}")


if __name__ == "__main__":
    unittest.main()
