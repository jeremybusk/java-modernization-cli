import tempfile
import unittest
from pathlib import Path

from javamod import discover
from javamod.errors import ModError

MAVEN_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId><artifactId>demo</artifactId><version>1.0</version>
  <properties>
    <maven.compiler.release>8</maven.compiler.release>
    <junit.version>4.13.2</junit.version>
  </properties>
  <dependencies>
    <dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>${junit.version}</version></dependency>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter</artifactId><version>2.7.18</version></dependency>
    <dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId><version>1.18.30</version></dependency>
  </dependencies>
</project>
"""

GRADLE_BUILD = """
plugins { id 'java' }
sourceCompatibility = '1.8'
dependencies {
    implementation 'com.google.guava:guava:31.1-jre'
    testImplementation 'org.junit.jupiter:junit-jupiter:5.10.0'
}
"""


class MavenDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "pom.xml").write_text(MAVEN_POM, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_root_build_and_java_version(self):
        build = discover.find_build_root(self.root, relative=None)
        self.assertEqual(build.tool, "maven")
        self.assertEqual(build.current_java, 8)

    def test_resolves_property_placeholder_version(self):
        build = discover.find_build_root(self.root, relative=None)
        junit = next(d for d in build.dependencies if d.artifact == "junit")
        self.assertEqual(junit.version, "4.13.2")

    def test_detects_features_and_spring_boot_version(self):
        build = discover.find_build_root(self.root, relative=None)
        self.assertEqual(build.features, {"junit4", "spring-boot", "lombok"})
        self.assertEqual(build.spring_boot, "2.7.18")


class GradleDiscoveryTests(unittest.TestCase):
    def test_finds_build_root_and_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build.gradle").write_text(GRADLE_BUILD, encoding="utf-8")
            build = discover.find_build_root(root, relative=None)
            self.assertEqual(build.tool, "gradle")
            self.assertEqual(build.current_java, 8)
            self.assertIn("guava", build.features)
            self.assertIn("junit5", build.features)


POM_SPRING_BOOT_PARENT = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>1.5.9.RELEASE</version>
  </parent>
  <groupId>com.example</groupId><artifactId>demo</artifactId><version>1.0</version>
  <properties><java.version>1.8</java.version></properties>
  <dependencies>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
  </dependencies>
</project>
"""

GRADLE_BOOT_PLUGIN = """
plugins {
    id 'org.springframework.boot' version '2.3.0.RELEASE'
    id 'java'
}
"""


class SpringBootParentDetectionTests(unittest.TestCase):
    def test_maven_parent_managed_boot_version_is_detected(self):
        # The common real-world shape: starters have no explicit <version>
        # (inherited from the parent BOM), so the version has to come from
        # <parent>, not from scanning <dependencies>.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pom.xml").write_text(POM_SPRING_BOOT_PARENT, encoding="utf-8")
            build = discover.find_build_root(root, relative=None)
            self.assertEqual(build.spring_boot, "1.5.9.RELEASE")
            self.assertIn("spring-boot", build.features)

    def test_gradle_boot_plugin_version_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build.gradle").write_text(GRADLE_BOOT_PLUGIN, encoding="utf-8")
            build = discover.find_build_root(root, relative=None)
            self.assertEqual(build.spring_boot, "2.3.0.RELEASE")
            self.assertIn("spring-boot", build.features)


class BuildRootSelectionTests(unittest.TestCase):
    def test_build_root_cannot_escape_via_parent_or_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "pom.xml").write_text(MAVEN_POM)
            (root / "link").symlink_to(outside, target_is_directory=True)
            for relative in ("../outside", "link", str(outside)):
                with self.subTest(relative=relative), self.assertRaisesRegex(ModError, "within"):
                    discover.find_build_root(root, relative)

    def test_independent_builds_at_different_depths_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in ("app-a", "services/app-b"):
                path = root / relative
                path.mkdir(parents=True)
                (path / "pom.xml").write_text(MAVEN_POM)
            with self.assertRaisesRegex(ModError, "multiple independent"):
                discover.find_build_root(root, None)

    def test_source_scanner_prunes_generated_and_external_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            for relative in ("B.java", "A.java", "target/Generated.java", ".cache/Hidden.java"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("class A {}")
            outside = Path(tmp) / "Outside.java"
            outside.write_text("class Outside {}")
            (root / "Linked.java").symlink_to(outside)
            self.assertEqual([p.name for p in discover.java_sources(root)], ["A.java", "B.java"])

    def test_errors_when_nothing_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ModError):
                discover.find_build_root(Path(tmp), relative=None)

    def test_errors_on_ambiguous_independent_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "service-a").mkdir()
            (root / "service-a" / "pom.xml").write_text(MAVEN_POM, encoding="utf-8")
            (root / "service-b").mkdir()
            (root / "service-b" / "pom.xml").write_text(MAVEN_POM, encoding="utf-8")
            with self.assertRaises(ModError):
                discover.find_build_root(root, relative=None)

    def test_explicit_build_root_resolves_ambiguity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "service-a").mkdir()
            (root / "service-a" / "pom.xml").write_text(MAVEN_POM, encoding="utf-8")
            (root / "service-b").mkdir()
            (root / "service-b" / "pom.xml").write_text(MAVEN_POM, encoding="utf-8")
            build = discover.find_build_root(root, relative="service-a")
            self.assertEqual(build.path, (root / "service-a").resolve())


class VersionAndModuleTests(unittest.TestCase):
    def _maven(self, root: Path, properties: str = "", modules: str = "", deps: str = "") -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "pom.xml").write_text(
            '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
            f"<properties>{properties}</properties><modules>{modules}</modules>"
            f"<dependencies>{deps}</dependencies></project>", encoding="utf-8")

    def test_two_digit_java_versions_are_not_truncated(self):
        for value, expected in (("1.8", 8), ("11", 11), ("17", 17), ("21", 21)):
            with tempfile.TemporaryDirectory() as tmp:
                self._maven(Path(tmp), properties=f"<java.version>{value}</java.version>")
                self.assertEqual(discover.find_build_root(Path(tmp), None).current_java, expected, value)

    def test_gradle_java_version_constant(self):
        for value, expected in (("VERSION_1_8", 8), ("VERSION_11", 11), ("VERSION_17", 17)):
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "build.gradle").write_text(f"sourceCompatibility = JavaVersion.{value}\n")
                self.assertEqual(discover.find_build_root(Path(tmp), None).current_java, expected, value)

    def test_maven_modules_contribute_dependencies_and_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._maven(root, properties="<guava.version>29.0-jre</guava.version>",
                        modules="<module>svc</module>")
            self._maven(root / "svc", deps="<dependency><groupId>com.google.guava</groupId>"
                                           "<artifactId>guava</artifactId><version>${guava.version}</version></dependency>")
            test = root / "svc/src/test/java/a/ATest.java"
            test.parent.mkdir(parents=True)
            test.write_text("package a;\nimport org.junit.Test;\nimport static org.mockito.Mockito.mock;\n"
                            "import javax.persistence.Entity;\nimport javax.crypto.Cipher;\nclass ATest {}\n")
            # A pom.xml that's a test fixture, not a module, must not be read.
            fixture = root / "svc/src/test/resources/pom.xml"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("not xml at all")
            build = discover.find_build_root(root, None)
        self.assertEqual({"guava", "junit4", "mockito", "javax"} - build.features, set())
        self.assertIn(discover.Dependency("com.google.guava", "guava", "29.0-jre"), build.dependencies)

    def test_jdk_javax_and_junit5_imports_do_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._maven(root)
            source = root / "src/test/java/a/ATest.java"
            source.parent.mkdir(parents=True)
            source.write_text("import javax.crypto.Cipher;\nimport org.junit.jupiter.api.Test;\nclass ATest {}\n")
            build = discover.find_build_root(root, None)
        self.assertEqual(build.features & {"javax", "junit4"}, set())
        self.assertIn("junit5", build.features)

    def test_gradle_subprojects_contribute_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build.gradle").write_text("")
            (root / "app").mkdir()
            (root / "app/build.gradle").write_text("dependencies { implementation 'com.google.guava:guava:29.0-jre' }\n")
            build = discover.find_build_root(root, None)
        self.assertIn("guava", build.features)


if __name__ == "__main__":
    unittest.main()
