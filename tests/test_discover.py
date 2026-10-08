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


if __name__ == "__main__":
    unittest.main()
