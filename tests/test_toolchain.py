import contextlib
import io
import os
import subprocess
import unittest
from unittest import mock

from javamod import cli, toolchain


class ToolchainTests(unittest.TestCase):
    def test_version_outputs(self):
        cases = [("java", 'openjdk version "21.0.2" 2024-01-16', (21, 0, 2)),
                 ("java", 'java version "1.8.0_442"', (8, 0)),
                 ("javac", "javac 17.0.13", (17, 0, 13)),
                 ("mvn", "\x1b[1mApache Maven 3.8.7\x1b[0m", (3, 8, 7)),
                 ("gradle", "Welcome to Gradle!\n\nGradle 8.14.5\n", (8, 14, 5))]
        for name, output, expected in cases:
            with self.subTest(name=name, output=output), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch("shutil.which", return_value=f"/bin/{name}"), \
                    mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=output)):
                self.assertEqual(toolchain.probe(name)[0], expected)

    def test_failed_and_unparseable_versions_are_not_accepted(self):
        for status, output in [(1, "Gradle 8.5"), (0, "unexpected output")]:
            with self.subTest(status=status), mock.patch("shutil.which", return_value="/bin/gradle"), \
                    mock.patch("subprocess.run", return_value=mock.Mock(returncode=status, stdout=output)):
                self.assertIsNone(toolchain.probe("gradle")[0])
        with mock.patch("shutil.which", return_value="/bin/gradle"), \
                mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gradle", 30)):
            self.assertIsNone(toolchain.probe("gradle")[0])

    def test_java_home_is_used(self):
        with mock.patch.dict(os.environ, {"JAVA_HOME": "/custom/jdk"}), \
                mock.patch("shutil.which", return_value=None) as which:
            toolchain.probe("javac")
        which.assert_called_once_with("/custom/jdk/bin/javac")

    def test_gradle_jdk_boundaries(self):
        cases = [("gradle", (4, 4, 1), 21, 21, False),
                 ("gradle", (8, 4), 21, 21, False),
                 ("gradle", (8, 5), 21, 21, True),
                 ("gradle", (8, 14, 5), 25, 25, False),
                 ("gradle", (9, 1), 25, 25, True),
                 ("gradle", (8, 5), 17, 25, False),
                 ("gradle", (9, 1), 11, 11, False),
                 ("gradle", (8, 14, 5), 11, 11, True),
                 ("mvn", (3, 6, 0), 21, 21, False),
                 ("mvn", (3, 6, 1), 21, 21, True),
                 ("javac", (17,), 21, 21, False)]
        for name, version, java, runtime, ok in cases:
            with self.subTest(name=name, version=version, java=java, runtime=runtime):
                self.assertEqual(toolchain.requirement(name, version, java=java, runtime=runtime) is None, ok)

    def test_doctor_requires_selected_tool_but_auto_accepts_either(self):
        versions = {"java": (21, 0, 2), "javac": (21, 0, 2), "mvn": (3, 8, 7), "gradle": (4, 4, 1)}
        for selected, expected in [("auto", 0), ("maven", 0), ("gradle", 1)]:
            with self.subTest(selected=selected), \
                    mock.patch("javamod.toolchain.probe", side_effect=lambda name: (versions[name], name)), \
                    mock.patch("shutil.which", side_effect=lambda name: "/bin/git" if name == "git" else None), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["doctor", "--build-tool", selected, "--java", "21"]), expected)
                self.assertIn("requires Gradle 8.5+", out.getvalue())

    def test_doctor_rejects_old_jdk_and_invalid_environment(self):
        with mock.patch("javamod.toolchain.probe", return_value=((17,), "17")), \
                mock.patch("shutil.which", return_value="/bin/tool"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["doctor", "--java", "21"]), 1)
        with mock.patch.dict(os.environ, {"JAVAMOD_BUILD_TOOL": "invalid"}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["doctor"]), 2)
