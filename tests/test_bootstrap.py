"""Exercise the actual bootstrap shell flow without network or host writes."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        self.system_jvms = self.root / "usr/lib/jvm"
        # Use a temporary distro JDK directory; keep the install flow unchanged.
        (self.root / "scripts/bootstrap.sh").write_text(
            (REPO / "scripts/bootstrap.sh").read_text().replace("/usr/lib/jvm/", f"{self.system_jvms}/"))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.jdk = self.root / "jdk"
        (self.jdk / "bin").mkdir(parents=True)
        self.log = self.root / "downloads"
        self.env = {**os.environ, "PATH": f"{self.bin}:/usr/bin:/bin", "JAVA_HOME": str(self.jdk),
                    "PYTHONPATH": str(REPO), "REAL_PYTHON": sys.executable,
                    "JAVAMOD_JAVA": "21", "JAVAMOD_GRADLE_VERSION": "8.14.5",
                    "DOWNLOAD_LOG": str(self.log), "FAKE_GRADLE": "4.4.1"}
        self.script(self.jdk / "bin/java", 'echo \'openjdk version "21.0.2"\'')
        self.script(self.jdk / "bin/javac", 'echo "javac 21.0.2"')
        for name in ("git", "pip3", "apt-get", "sudo"):
            self.script(self.bin / name, "exit 0")
        self.script(self.bin / "dpkg-query", "echo 'install ok installed'")
        self.script(self.bin / "mvn", 'echo "Apache Maven 3.8.7"')
        self.script(self.bin / "gradle", 'echo "Gradle $FAKE_GRADLE"')
        self.script(self.bin / "python3", '''
if [ "$1" = -m ] && [ "$2" = venv ]; then
  mkdir -p .venv/bin
  ln -sfn "$REAL_PYTHON" .venv/bin/python
  printf '#!/bin/sh\nexit 0\n' > .venv/bin/pip
  printf '#!/bin/sh\nexec "$REAL_PYTHON" -m javamod.cli "$@"\n' > .venv/bin/javamod
  chmod +x .venv/bin/pip .venv/bin/javamod
else
  exec "$REAL_PYTHON" "$@"
fi
''')
        checksum = hashlib.sha256(b"fake archive").hexdigest()
        self.script(self.bin / "curl", f'''
echo "$*" >> "$DOWNLOAD_LOG"
url="${{5}}"
out="${{7}}"
case "$url" in
  *.sha256) printf '%s' "${{FAKE_CHECKSUM:-{checksum}}}" > "$out" ;;
  *) printf 'fake archive' > "$out" ;;
esac
''')
        self.script(self.bin / "unzip", '''
version="${3##*/gradle-}"
version="${version%-bin.zip}"
mkdir -p "$5/gradle-$version/bin"
printf '#!/bin/sh\necho "Gradle %s"\n' "$version" > "$5/gradle-$version/bin/gradle"
chmod +x "$5/gradle-$version/bin/gradle"
''')

    def script(self, path, body):
        path.write_text("#!/bin/bash\nset -eu\n" + body + "\n")
        path.chmod(0o755)

    def run_bootstrap(self):
        return subprocess.run(["bash", str(self.root / "scripts/bootstrap.sh")], cwd=self.root,
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_old_gradle_is_installed_and_second_run_skips_download(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / ".venv/bin/gradle").is_symlink())
        self.assertIn("Gradle 8.14.5", result.stdout)
        downloads = self.log.read_text()
        self.assertIn("gradle-8.14.5-bin.zip.sha256", downloads)
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text(), downloads)

    def test_compatible_gradle_is_preserved(self):
        self.env["FAKE_GRADLE"] = "8.5"
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.root / ".venv/bin/gradle").exists())

    def test_checksum_mismatch_stops_before_installation(self):
        self.env["FAKE_CHECKSUM"] = "0" * 64
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / ".venv/bin/gradle").exists())
        self.assertIn("FAILED", result.stdout)

    def test_java_25_gets_gradle_9(self):
        self.env["JAVAMOD_JAVA"] = "25"
        self.env.pop("JAVAMOD_GRADLE_VERSION")
        self.script(self.jdk / "bin/java", 'echo \'openjdk version "25.0.1"\'')
        self.script(self.jdk / "bin/javac", 'echo "javac 25.0.1"')
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gradle-9.7.1-bin.zip", self.log.read_text())

    def test_incompatible_override_fails_the_final_check(self):
        self.env["JAVAMOD_GRADLE_VERSION"] = "7.6.4"
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires Gradle 8.5+", result.stdout)

    def test_absent_target_jdk_is_resolved_after_package_install(self):
        old_jdk = self.root / "old-jdk/bin"
        old_jdk.mkdir(parents=True)
        self.script(old_jdk / "javac", 'echo "javac 17.0.2"')
        self.env["JAVA_HOME"] = str(old_jdk.parent)
        self.env["TARGET_JDK"] = str(self.system_jvms / "java-21-openjdk-amd64")
        self.env["JDK_FIXTURE"] = str(self.jdk)
        self.script(self.bin / "sudo", '''
if [ "$2" = install ]; then
  mkdir -p "$TARGET_JDK"
  cp -r "$JDK_FIXTURE/bin" "$TARGET_JDK/"
fi
''')
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("openjdk-21-jdk", result.stdout)
        self.assertIn(self.env["TARGET_JDK"], result.stdout)
