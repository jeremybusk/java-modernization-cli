import unittest
from types import SimpleNamespace

from javamod import ai


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


class FileErrorRegexTests(unittest.TestCase):
    def test_matches_typical_javac_error_lines(self):
        output = "src/main/java/com/example/App.java:42: error: cannot find symbol\nmore noise"
        matches = [m.group("path") for m in ai.FILE_ERROR_RE.finditer(output)]
        self.assertEqual(matches, ["src/main/java/com/example/App.java"])


if __name__ == "__main__":
    unittest.main()
