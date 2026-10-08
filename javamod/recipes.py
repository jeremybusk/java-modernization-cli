"""Decide *what* to run: a list of OpenRewrite recipe modules, grouped into
named phases, tailored to what :mod:`javamod.discover` found in the repo.

Recipe binaries come from one of three places (``--recipe-source``):

* ``maven-central`` (default) -- the real, publicly published
  ``org.openrewrite.recipe:*`` artifacts. No token, no network beyond Maven
  Central, no extra build step.
* ``source`` -- clone the matching tag of the actual open-source OpenRewrite
  recipe repository on GitHub and build it locally with
  ``./gradlew publishToMavenLocal``. Slower, but you are running code you
  can read, from the project's own repositories.
* ``codegenome`` -- Moderne's private Code Genome artifact server. This is
  never reached unless the caller passes *both* ``--recipe-source
  codegenome`` *and* ``--allow-codegenome``, so it can't be pulled in
  silently by another mode's fallback.
"""
from __future__ import annotations

import dataclasses
import re

from .discover import BuildRoot
from .errors import ModError

MAVEN_CENTRAL_VERSIONS = {
    "migrate_java": "3.42.1",
    "static_analysis": "2.41.1",
    "testing_frameworks": "3.44.0",
    "java_dependencies": "1.60.2",
    "spring": "6.37.1",  # verified against repo.maven.apache.org's maven-metadata.xml <release>
}
# Only reachable with --recipe-source codegenome --allow-codegenome.
CODEGENOME_VERSIONS = {
    "migrate_java": "3.45.0",
    "static_analysis": "2.44.0",
    "testing_frameworks": "3.47.0",
    "java_dependencies": "1.63.0",
    "spring": "6.43.0",
}
ARTIFACT_IDS = {
    "migrate_java": "rewrite-migrate-java",
    "static_analysis": "rewrite-static-analysis",
    "testing_frameworks": "rewrite-testing-frameworks",
    "java_dependencies": "rewrite-java-dependencies",
    "spring": "rewrite-spring",
}
# --recipe-source source builds exactly these public repos at tag v<version>.
SOURCE_REPOS = {
    "migrate_java": "openrewrite/rewrite-migrate-java",
    "static_analysis": "openrewrite/rewrite-static-analysis",
    "testing_frameworks": "openrewrite/rewrite-testing-frameworks",
    "java_dependencies": "openrewrite/rewrite-java-dependencies",
    "spring": "openrewrite/rewrite-spring",
}
# The plugins that *execute* recipes. Always resolved from Maven Central /
# the Gradle Plugin Portal -- even in "source" and "codegenome" modes, which
# only change where the *recipe* jars above come from.
PLUGIN_VERSIONS = {
    "maven-central": {"maven": "6.46.1", "gradle": "7.39.0"},
    "source": {"maven": "6.46.1", "gradle": "7.39.0"},
    "codegenome": {"maven": "6.49.0", "gradle": "7.41.0"},
}
CODEGENOME_URL = "https://artifacts.codegenomeproject.org/maven"
TARGET_JAVA_VERSIONS = (11, 17, 21, 25)

PROFILE_DEFAULTS = {
    # Java/JDK-API migration recipes always run; profiles only control the
    # optional phases layered on top.
    "conservative": {"cleanup": False, "modernize_tests": False, "best_practices": False},
    "standard": {"cleanup": True, "modernize_tests": True, "best_practices": True},
    "aggressive": {"cleanup": True, "modernize_tests": True, "best_practices": True, "upgrade_dependencies": True},
}

_DEPENDENCY_UPGRADE_SKIP = (
    "org.springframework", "org.junit", "junit", "org.mockito", "org.projectlombok",
    "org.hibernate", "com.fasterxml.jackson",
)


@dataclasses.dataclass(frozen=True)
class MigrationPhase:
    name: str
    recipes: tuple[str, ...]


@dataclasses.dataclass
class Plan:
    modules: list[str]
    phases: list[MigrationPhase]

    @property
    def recipe_names(self) -> list[str]:
        return [recipe.splitlines()[0] for phase in self.phases for recipe in phase.recipes]


def build_plan(build: BuildRoot, *, target_java: int, boot: str | None, profile: str,
               dependency_strategy: str = "patch", extra_recipes: tuple[str, ...] = ()) -> Plan:
    if target_java not in TARGET_JAVA_VERSIONS:
        raise ModError(f"--java must be one of {TARGET_JAVA_VERSIONS}, got {target_java}")
    if profile not in PROFILE_DEFAULTS:
        raise ModError(f"--profile must be one of {sorted(PROFILE_DEFAULTS)}, got {profile!r}")
    opts = PROFILE_DEFAULTS[profile]
    modules = {"migrate_java"}
    phases = [MigrationPhase("java-upgrade", (f"org.openrewrite.java.migrate.UpgradeToJava{target_java}",))]

    compatibility = []
    if "javax" in build.features:
        compatibility.append("org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta")
    if {"lombok", "mapstruct"} <= build.features:
        compatibility.append("org.openrewrite.java.migrate.AddLombokMapstructBinding")
    if compatibility:
        phases.append(MigrationPhase("compatibility", tuple(compatibility)))

    if boot and build.spring_boot:
        modules.add("spring")
        phases.append(MigrationPhase("spring-boot", (spring_boot_recipe(boot),)))

    if opts["modernize_tests"] and {"junit4", "mockito"} & build.features:
        modules.add("testing_frameworks")
        migration = tuple(r for r in (
            "org.openrewrite.java.testing.junit5.JUnit4to5Migration" if "junit4" in build.features else None,
        ) if r)
        if migration:
            phases.append(MigrationPhase("testing-migration", migration))
        phases.append(MigrationPhase("testing-cleanup", ("org.openrewrite.java.testing.junit5.JUnit5BestPractices",)))

    if opts.get("upgrade_dependencies"):
        dependency_recipes = _dependency_recipes(build, dependency_strategy)
        if dependency_recipes:
            modules.add("java_dependencies")
            phases.append(MigrationPhase("dependencies", dependency_recipes))

    if opts["cleanup"]:
        modules.add("static_analysis")
        phases.append(MigrationPhase("cleanup", ("org.openrewrite.staticanalysis.CommonStaticAnalysis",)))
    if opts["best_practices"]:
        modules.add("static_analysis")
        best_practice = "org.openrewrite.maven.BestPractices" if build.tool == "maven" else "org.openrewrite.gradle.GradleBestPractices"
        phases.append(MigrationPhase("best-practices", (best_practice,)))
    if extra_recipes:
        phases.append(MigrationPhase("custom", extra_recipes))
    return Plan(modules=sorted(modules), phases=phases)


def parse_boot_version(value: str) -> tuple[int, int]:
    """Extract (major, minor) from the leading MAJOR.MINOR of *value*.

    A deliberate prefix match, not a full match: the detected version from a
    dependency declaration is a full artifact version like ``2.3.0.RELEASE``
    or ``1.5.9.RELEASE``, not the bare ``MAJOR.MINOR`` a --boot flag uses --
    both need to parse here.
    """
    match = re.match(r"(\d+)\.(\d+)", value.strip())
    if not match:
        raise ModError(f"Spring Boot version must start with MAJOR.MINOR (e.g. 3.5), got {value!r}")
    return int(match.group(1)), int(match.group(2))


def spring_boot_recipe(target: str) -> str:
    """The one recipe for the exact target version.

    OpenRewrite's UpgradeSpringBoot_X_Y recipes are fully cumulative, not
    just within a major version but *across* them: UpgradeSpringBoot_3_0's
    own recipeList starts with UpgradeSpringBoot_2_7, whose list starts with
    _2_6, ... down through UpgradeSpringBoot_2_0, which is itself documented
    as "Migrate from Spring Boot 1.x to 2.0". UpgradeSpringBoot_4_0 likewise
    starts with UpgradeSpringBoot_3_5. So the single target recipe handles a
    project starting from Spring Boot 1.5 reaching 3.5, or further, in one
    pass -- verified directly against openrewrite/rewrite-spring's recipe
    YAML and with a real migration run, not assumed. No staging needed.
    """
    major, minor = parse_boot_version(target)
    return f"org.openrewrite.java.spring.boot{major}.UpgradeSpringBoot_{major}_{minor}"


def _dependency_recipes(build: BuildRoot, strategy: str) -> tuple[str, ...]:
    new_version = {"patch": "latest.patch", "latest": "latest.release"}.get(strategy)
    if new_version is None:
        raise ModError(f"--dependency-strategy must be 'patch' or 'latest', got {strategy!r}")
    seen: set[tuple[str, str]] = set()
    recipes = []
    for dep in build.dependencies:
        key = (dep.group, dep.artifact)
        if key in seen or dep.group.startswith(_DEPENDENCY_UPGRADE_SKIP):
            continue
        seen.add(key)
        recipes.append(
            "org.openrewrite.java.dependencies.UpgradeDependencyVersion:\n"
            f"      groupId: {dep.group}\n      artifactId: {dep.artifact}\n      newVersion: {new_version}"
        )
    return tuple(recipes)


def resolve_artifacts(modules: list[str], recipe_source: str, allow_codegenome: bool) -> list[str]:
    if recipe_source == "codegenome" and not allow_codegenome:
        raise ModError("--recipe-source codegenome also requires the explicit --allow-codegenome flag")
    versions = CODEGENOME_VERSIONS if recipe_source == "codegenome" else MAVEN_CENTRAL_VERSIONS
    return [f"org.openrewrite.recipe:{ARTIFACT_IDS[m]}:{versions[m]}" for m in modules]
