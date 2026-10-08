import unittest

from javamod import triage

MAVEN_OUTPUT = """[ERROR] /app/src/main/java/com/example/Widget.java:[13,52] package springfox.documentation.swagger2.annotations does not exist
[ERROR] /app/src/main/java/com/example/Service.java:[9,48] cannot find symbol
[ERROR]   symbol:   class CounterService
[ERROR]   location: package org.springframework.boot.actuate.metrics
[ERROR] /app/src/main/java/com/example/Service.java:[40,31] cannot find symbol
[ERROR]   symbol:   method findOne(long)
[ERROR]   location: variable repo of type com.example.WidgetRepository
[ERROR] /app/src/main/java/com/example/Other.java:[7,1] some entirely novel problem nobody has a pattern for
[INFO] 3 errors
[INFO] BUILD FAILURE
[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.16.0:compile (default-compile) on project app: Compilation failure: Compilation failure:
[ERROR] /app/src/main/java/com/example/Widget.java:[13,52] package springfox.documentation.swagger2.annotations does not exist
[ERROR] /app/src/main/java/com/example/Service.java:[9,48] cannot find symbol
[ERROR] /app/src/main/java/com/example/Service.java:[40,31] cannot find symbol
[ERROR] /app/src/main/java/com/example/Other.java:[7,1] some entirely novel problem nobody has a pattern for
[ERROR] -> [Help 1]
"""

GRADLE_OUTPUT = """> Task :compileJava FAILED
/app/src/main/java/com/example/Widget.java:13: error: package springfox.documentation.swagger2.annotations does not exist
import springfox.documentation.swagger2.annotations.EnableSwagger2;
       ^
1 error
"""

TEST_FAILURE_OUTPUT = """Tests in error:
  HotelControllerTest.shouldCreateAndUpdateAndDelete » IllegalState Failed to load ApplicationContext
  HotelControllerTest.shouldCreateRetrieveDelete » IllegalState Failed to load ApplicationContext

Tests run: 2, Failures: 0, Errors: 2, Skipped: 0
"""

# Shape taken from a real failure: an OpenRewrite Spring Boot recipe stripped
# an explicit <version> it assumed Boot's own BOM would supply, but doesn't.
MAVEN_POM_VALIDATION_OUTPUT = """[ERROR] [ERROR] Some problems were encountered while processing the POMs:
[ERROR] 'dependencies.dependency.version' for de.flapdoodle.embed:de.flapdoodle.embed.mongo:jar is missing. @ line 58, column 15
[ERROR] 'dependencies.dependency.version' for de.flapdoodle.embed:de.flapdoodle.embed.mongo:jar is missing. @ line 78, column 15
 @
[ERROR] The build could not read 2 projects -> [Help 1]
[ERROR]
[ERROR]   The project com.example:auth-service:1.0-SNAPSHOT (/app/auth-service/pom.xml) has 1 error
[ERROR]     'dependencies.dependency.version' for de.flapdoodle.embed:de.flapdoodle.embed.mongo:jar is missing. @ line 58, column 15
[ERROR]
[ERROR]   The project com.example:account-service:1.0-SNAPSHOT (/app/account-service/pom.xml) has 1 error
[ERROR]     'dependencies.dependency.version' for de.flapdoodle.embed:de.flapdoodle.embed.mongo:jar is missing. @ line 78, column 15
[ERROR]
[ERROR] To see the full stack trace of the errors, re-run Maven with the -e switch.
"""


# Real Surefire 3 output from a piggymetrics migration run (trimmed), plus an
# Errors block in the shape Surefire prints for exceptions and flaky reruns.
SUREFIRE_SUMMARY_OUTPUT = """[INFO] Results:
[INFO]
[ERROR] Failures:
[ERROR]   ExchangeRatesClientTest.shouldRetrieveExchangeRates:26 expected: <null> but was: <USD>
[ERROR]   ExchangeRatesClientTest.shouldRetrieveExchangeRatesForSpecifiedCurrency:41 expected: <null> but was: <USD>
[ERROR] Errors:
[ERROR]   AppTest.contextLoads \u00bb IllegalState Failed to load ApplicationContext
[ERROR]   FlakyTest.sometimes
[ERROR]   Run 1: FlakyTest.sometimes:10 expected: <1> but was: <2>
[ERROR]   Run 2: FlakyTest.sometimes:10 expected: <1> but was: <2>
[INFO]
[ERROR] Tests run: 16, Failures: 2, Errors: 2, Skipped: 0
[INFO] BUILD FAILURE
"""

# Real Gradle 4.4 output (from verifying javamod's --skip-test exclusion).
GRADLE_TEST_OUTPUT = """demo.BadTest$Inner > bad2 FAILED
    java.lang.AssertionError at BadTest.java:2

demo.BadTest > bad FAILED
    java.lang.AssertionError at BadTest.java:1

2 tests completed, 2 failed
:test FAILED
"""


class TestSummaryParsingTests(unittest.TestCase):
    def test_surefire_summary_entries_become_test_failure_issues(self):
        issues = triage.parse_build_failures("maven", SUREFIRE_SUMMARY_OUTPUT)
        self.assertEqual([i["file"] for i in issues], [
            "ExchangeRatesClientTest.shouldRetrieveExchangeRates",
            "ExchangeRatesClientTest.shouldRetrieveExchangeRatesForSpecifiedCurrency",
            "AppTest.contextLoads",
            "FlakyTest.sometimes",
        ])
        first = issues[0]
        self.assertEqual(first["category"], "test-failure")
        self.assertEqual(first["lines"], [26])
        self.assertEqual(first["message"], "expected: <null> but was: <USD>")
        self.assertIsNone(first["recommended_fix"])
        self.assertIn("IllegalState", issues[2]["message"])

    def test_gradle_test_failures_are_parsed(self):
        issues = triage.parse_build_failures("gradle", GRADLE_TEST_OUTPUT)
        self.assertEqual([(i["file"], i["lines"], i["category"]) for i in issues], [
            ("demo.BadTest$Inner.bad2", [2], "test-failure"),
            ("demo.BadTest.bad", [1], "test-failure"),
        ])
        self.assertEqual(issues[0]["message"], "java.lang.AssertionError")


class MavenParsingTests(unittest.TestCase):
    def test_known_patterns_are_matched_with_fix_text(self):
        issues = triage.parse_build_failures("maven", MAVEN_OUTPUT)
        by_category = {i["category"] for i in issues}
        self.assertIn("unresolved-dependency", by_category)
        self.assertIn("removed-api", by_category)
        self.assertIn("renamed-api", by_category)
        springfox = next(i for i in issues if i["category"] == "unresolved-dependency")
        self.assertIn("springdoc-openapi", springfox["recommended_fix"])
        self.assertEqual(springfox["confidence"], "high")

    def test_unmatched_issue_gets_null_fix_not_a_guess(self):
        issues = triage.parse_build_failures("maven", MAVEN_OUTPUT)
        novel = next(i for i in issues if "novel problem" in i["message"])
        self.assertIsNone(novel["recommended_fix"])
        self.assertIsNone(novel["likely_cause"])
        self.assertEqual(novel["confidence"], "unverified")
        self.assertEqual(novel["category"], "unknown")

    def test_duplicate_summary_block_does_not_double_count_issues(self):
        # MAVEN_OUTPUT repeats every error once in the goal-failure summary.
        issues = triage.parse_build_failures("maven", MAVEN_OUTPUT)
        self.assertEqual(len(issues), 4)

    def test_lines_are_collected_without_duplicates(self):
        issues = triage.parse_build_failures("maven", MAVEN_OUTPUT)
        counter_service = next(i for i in issues if i["category"] == "removed-api")
        self.assertEqual(counter_service["lines"], [9])


class MavenPomValidationParsingTests(unittest.TestCase):
    def test_each_affected_module_is_a_distinct_located_issue(self):
        issues = triage.parse_build_failures("maven", MAVEN_POM_VALIDATION_OUTPUT)
        self.assertEqual(len(issues), 2)
        files = {i["file"] for i in issues}
        self.assertEqual(files, {"/app/auth-service/pom.xml", "/app/account-service/pom.xml"})

    def test_matched_as_the_known_pattern_with_fix_text(self):
        issues = triage.parse_build_failures("maven", MAVEN_POM_VALIDATION_OUTPUT)
        self.assertTrue(all(i["category"] == "missing-dependency-version" for i in issues))
        self.assertTrue(all(i["recommended_fix"] for i in issues))
        self.assertTrue(all(i["confidence"] == "high" for i in issues))

    def test_line_numbers_are_correct_per_file(self):
        issues = triage.parse_build_failures("maven", MAVEN_POM_VALIDATION_OUTPUT)
        by_file = {i["file"]: i["lines"] for i in issues}
        self.assertEqual(by_file["/app/auth-service/pom.xml"], [58])
        self.assertEqual(by_file["/app/account-service/pom.xml"], [78])


class GradleParsingTests(unittest.TestCase):
    def test_compile_paths_with_spaces_are_preserved(self):
        for tool, output in (("gradle", "/my app/A.java:12: error: cannot find symbol"),
                             ("maven", "[ERROR] /my app/A.java:[12,1] cannot find symbol")):
            with self.subTest(tool=tool):
                issues = triage.parse_build_failures(tool, output)
                self.assertEqual(issues[0]["file"], "/my app/A.java")
                self.assertEqual(issues[0]["lines"], [12])

    def test_gradle_error_format_is_parsed(self):
        issues = triage.parse_build_failures("gradle", GRADLE_OUTPUT)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["category"], "unresolved-dependency")
        self.assertEqual(issues[0]["lines"], [13])


class TestFailureParsingTests(unittest.TestCase):
    def test_test_failures_are_captured(self):
        issues = triage.parse_build_failures("maven", TEST_FAILURE_OUTPUT)
        self.assertEqual(len(issues), 2)
        self.assertTrue(all(i["category"] == "test-failure" for i in issues))
        self.assertIn("HotelControllerTest.shouldCreateAndUpdateAndDelete", issues[0]["file"])


class YamlRenderingTests(unittest.TestCase):
    def test_control_characters_are_escaped(self):
        import json
        value = "tab\tcarriage\rreturn\nnull\x00"
        rendered = triage._yaml_scalar(value)
        self.assertNotIn("\r", rendered)
        self.assertNotIn("\x00", rendered)
        self.assertEqual(json.loads(rendered), value)

    def test_empty_issue_list(self):
        self.assertEqual(triage.to_yaml([]), "issues: []\n")

    def test_renders_valid_structure_with_null_fields(self):
        issues = triage.parse_build_failures("maven", MAVEN_OUTPUT)
        text = triage.to_yaml(issues)
        self.assertIn('category: "unresolved-dependency"', text)
        self.assertIn("recommended_fix: null", text)

    def test_quotes_and_backslashes_in_messages_are_escaped(self):
        issues = [{
            "file": "A.java", "lines": [1], "category": "unknown",
            "message": 'cannot find "Foo" in C:\\path', "likely_cause": None,
            "recommended_fix": None, "confidence": "unverified",
        }]
        text = triage.to_yaml(issues)
        self.assertIn('\\"Foo\\"', text)
        self.assertIn("C:\\\\path", text)


if __name__ == "__main__":
    unittest.main()
