import tempfile
import unittest
from pathlib import Path
from unittest import mock

from javamod import formatting
from javamod.discover import BuildRoot
from javamod.errors import ModError


def maven_build(pom_text: str) -> tuple[tempfile.TemporaryDirectory, BuildRoot]:
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name)
    (path / "pom.xml").write_text(pom_text, encoding="utf-8")
    return tmp, BuildRoot(path=path, tool="maven")


def gradle_build(build_text: str, filename: str = "build.gradle") -> tuple[tempfile.TemporaryDirectory, BuildRoot]:
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name)
    (path / filename).write_text(build_text, encoding="utf-8")
    return tmp, BuildRoot(path=path, tool="gradle")


class DetectGoalTests(unittest.TestCase):
    def test_no_formatter_detected(self):
        tmp, build = maven_build("<project></project>")
        with tmp:
            self.assertIsNone(formatting.detect_goal(build))

    def test_maven_spring_javaformat_detected(self):
        tmp, build = maven_build("<project><build><plugins><plugin>"
                                  "<artifactId>spring-javaformat-maven-plugin</artifactId>"
                                  "</plugin></plugins></build></project>")
        with tmp:
            self.assertEqual(formatting.detect_goal(build), "spring-javaformat:apply")

    def test_maven_spotless_detected(self):
        tmp, build = maven_build("<project><build><plugins><plugin>"
                                  "<artifactId>spotless-maven-plugin</artifactId>"
                                  "</plugin></plugins></build></project>")
        with tmp:
            self.assertEqual(formatting.detect_goal(build), "spotless:apply")

    def test_gradle_spring_javaformat_detected(self):
        tmp, build = gradle_build('plugins { id "io.spring.javaformat" version "0.0.47" }')
        with tmp:
            self.assertEqual(formatting.detect_goal(build), "format")

    def test_gradle_spotless_detected_in_kts(self):
        tmp, build = gradle_build('plugins { id("com.diffplug.spotless") version "6.0.0" }',
                                   filename="build.gradle.kts")
        with tmp:
            self.assertEqual(formatting.detect_goal(build), "spotlessApply")


class ReconcileTests(unittest.TestCase):
    def test_does_nothing_when_no_formatter_detected(self):
        tmp, build = maven_build("<project></project>")
        with tmp, mock.patch("subprocess.run") as mock_run:
            ran = formatting.reconcile(build)
            self.assertFalse(ran)
            mock_run.assert_not_called()

    def test_runs_the_detected_maven_goal(self):
        tmp, build = maven_build("<project><build><plugins><plugin>"
                                  "<artifactId>spotless-maven-plugin</artifactId>"
                                  "</plugin></plugins></build></project>")
        with tmp, mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="")) as mock_run:
            ran = formatting.reconcile(build)
            self.assertTrue(ran)
            command = mock_run.call_args.args[0]
            self.assertIn("spotless:apply", command)

    def test_raises_on_nonzero_exit(self):
        tmp, build = maven_build("<project><build><plugins><plugin>"
                                  "<artifactId>spotless-maven-plugin</artifactId>"
                                  "</plugin></plugins></build></project>")
        with tmp, mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout="boom")):
            with self.assertRaises(ModError):
                formatting.reconcile(build)


if __name__ == "__main__":
    unittest.main()
