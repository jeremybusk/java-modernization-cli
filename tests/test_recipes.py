import unittest
from pathlib import Path

from javamod import recipes
from javamod.discover import BuildRoot, Dependency
from javamod.errors import ModError


def build_root(tool="maven", features=frozenset(), spring_boot=None, dependencies=()):
    return BuildRoot(path=Path("/tmp/x"), tool=tool, current_java=8, spring_boot=spring_boot,
                      features=set(features), dependencies=list(dependencies))


class ProfileTests(unittest.TestCase):
    def test_conservative_skips_cleanup_and_tests(self):
        plan = recipes.build_plan(build_root(features={"junit4"}), target_java=21, boot=None, profile="conservative")
        names = plan.recipe_names
        self.assertTrue(any("UpgradeToJava21" in n for n in names))
        self.assertFalse(any("JUnit4to5" in n for n in names))
        self.assertFalse(any("CommonStaticAnalysis" in n for n in names))

    def test_standard_modernizes_tests_and_cleans_up(self):
        plan = recipes.build_plan(build_root(features={"junit4"}), target_java=21, boot=None, profile="standard")
        names = plan.recipe_names
        self.assertTrue(any("JUnit4to5Migration" in n for n in names))
        self.assertTrue(any("CommonStaticAnalysis" in n for n in names))
        self.assertTrue(any("BestPractices" in n for n in names))

    def test_only_aggressive_upgrades_dependencies(self):
        deps = [Dependency("com.example", "widgets", "1.0")]
        standard = recipes.build_plan(build_root(dependencies=deps), target_java=21, boot=None, profile="standard")
        aggressive = recipes.build_plan(build_root(dependencies=deps), target_java=21, boot=None, profile="aggressive")
        self.assertFalse(any("UpgradeDependencyVersion" in n for n in standard.recipe_names))
        self.assertTrue(any("UpgradeDependencyVersion" in n for n in aggressive.recipe_names))

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ModError):
            recipes.build_plan(build_root(), target_java=21, boot=None, profile="bogus")

    def test_unsupported_target_java_rejected(self):
        with self.assertRaises(ModError):
            recipes.build_plan(build_root(), target_java=99, boot=None, profile="standard")


class SpringBootTests(unittest.TestCase):
    def test_java_upgrade_preserves_legacy_boot_namespace(self):
        for boot in (None, "2.7"):
            plan = recipes.build_plan(build_root(features={"javax"}, spring_boot="2.3.0"),
                                      target_java=21, boot=boot, profile="conservative")
            self.assertFalse(any("JavaxMigrationToJakarta" in name for name in plan.recipe_names))

    def test_explicit_boot3_upgrade_migrates_namespace(self):
        plan = recipes.build_plan(build_root(features={"javax"}, spring_boot="2.7.18"),
                                  target_java=21, boot="3.5", profile="conservative")
        self.assertTrue(any("JavaxMigrationToJakarta" in name for name in plan.recipe_names))

    def test_boot3_target_rejects_java11(self):
        with self.assertRaisesRegex(ModError, "Java 17"):
            recipes.build_plan(build_root(spring_boot="2.7.18"), target_java=11, boot="3.5", profile="standard")

    def test_boot_recipe_only_applied_when_spring_detected(self):
        plan = recipes.build_plan(build_root(spring_boot=None), target_java=21, boot="3.5", profile="standard")
        self.assertFalse(any("UpgradeSpringBoot" in n for n in plan.recipe_names))

    def test_boot_recipe_applied_when_spring_present(self):
        plan = recipes.build_plan(build_root(spring_boot="2.7.18"), target_java=21, boot="3.5", profile="standard")
        self.assertTrue(any("UpgradeSpringBoot_3_5" in n for n in plan.recipe_names))

    def test_malformed_boot_target_rejected(self):
        with self.assertRaises(ModError):
            recipes.build_plan(build_root(spring_boot="2.7.18"), target_java=21, boot="latest", profile="standard")


class DependencyUpgradeSkipTests(unittest.TestCase):
    def test_coordinated_frameworks_are_left_to_dedicated_recipes(self):
        deps = [Dependency("org.springframework", "spring-core", "5.3.0"), Dependency("junit", "junit", "4.13.2")]
        plan = recipes.build_plan(build_root(dependencies=deps), target_java=21, boot=None, profile="aggressive")
        joined = "\n".join(plan.recipe_names)
        self.assertNotIn("spring-core", joined)
        self.assertNotIn("groupId: junit", joined)


class ArtifactResolutionTests(unittest.TestCase):
    def test_maven_central_is_the_default_and_needs_no_flag(self):
        coords = recipes.resolve_artifacts(["migrate_java"], "maven-central", allow_codegenome=False)
        self.assertEqual(coords, [f"org.openrewrite.recipe:rewrite-migrate-java:{recipes.MAVEN_CENTRAL_VERSIONS['migrate_java']}"])

    def test_codegenome_refused_without_explicit_opt_in(self):
        with self.assertRaises(ModError):
            recipes.resolve_artifacts(["migrate_java"], "codegenome", allow_codegenome=False)

    def test_codegenome_allowed_with_explicit_opt_in(self):
        coords = recipes.resolve_artifacts(["migrate_java"], "codegenome", allow_codegenome=True)
        self.assertIn(recipes.CODEGENOME_VERSIONS["migrate_java"], coords[0])


class SpringBootRecipeNamingTests(unittest.TestCase):
    def test_recipe_name_for_a_cross_major_jump_is_still_just_the_target(self):
        # No staging: org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_5's
        # own recipeList chains back through 3.4, 3.3, ..., 3.0, then boot2's
        # 2.7 down to 2.0 ("Migrate from Spring Boot 1.x to 2.0"), so one
        # recipe reaches 3.5 from a Spring Boot 1.5 project. Verified against
        # openrewrite/rewrite-spring's recipe YAML and a real migration run,
        # not assumed -- see recipes.spring_boot_recipe's docstring.
        self.assertEqual(recipes.spring_boot_recipe("3.5"), "org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_5")


if __name__ == "__main__":
    unittest.main()
