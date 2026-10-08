"""Find the build and work out what it already is, before planning changes.

One javamod run targets one build (a single Maven reactor or Gradle build,
possibly multi-module -- both tools apply recipes across every module from
the root in one invocation, so there is normally no need to enumerate
submodules separately). Monorepos with several *independent* applications
should be run once per application via ``--build-root``.
"""
from __future__ import annotations

import dataclasses
import os
import re
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Iterator
from pathlib import Path

from .errors import ModError

IGNORED_DIRS = {".git", "target", "build", ".gradle", "node_modules", "vendor"}
BUILD_FILES = {"maven": ("pom.xml",), "gradle": ("build.gradle", "build.gradle.kts")}

# Coordinates that flag a feature worth tailoring the recipe plan around.
FEATURE_MARKERS = {
    "lombok": ("org.projectlombok", "lombok"),
    "mapstruct": ("org.mapstruct", None),
    "guava": ("com.google.guava", "guava"),
    "junit4": ("junit", "junit"),
    "junit5": ("org.junit.jupiter", None),
    "mockito": ("org.mockito", None),
    "spring-boot": ("org.springframework.boot", None),
    "javax": ("javax.", None),
}


@dataclasses.dataclass(frozen=True)
class Dependency:
    group: str
    artifact: str
    version: str


@dataclasses.dataclass
class BuildRoot:
    path: Path
    tool: str  # "maven" | "gradle"
    current_java: int | None = None
    spring_boot: str | None = None
    features: set[str] = dataclasses.field(default_factory=set)
    dependencies: list[Dependency] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)


def find_build_root(repo: Path, relative: str | None, max_depth: int = 2, *, build_tool: str = "auto") -> BuildRoot:
    """Return the one build to migrate.

    *relative*, if given, pins the build root explicitly (for monorepos).
    Otherwise this looks at the repo root first, then one/two levels down,
    and fails loudly if zero or several independent roots are found there --
    pick a directory with ``--build-root``. If that directory contains both
    Maven and Gradle files, select the tool with ``--build-tool``.
    """
    if relative:
        candidate = (repo / relative).resolve()
        if not candidate.is_relative_to(repo.resolve()):
            raise ModError("--build-root must stay within the source repository")
        tool = _tool_at(candidate, build_tool)
        if not tool:
            raise ModError(f"no pom.xml or build.gradle[.kts] at --build-root {relative}")
        return _analyze(candidate, tool)

    if _build_files(repo):
        tool = _tool_at(repo, build_tool)
        assert tool
        return _analyze(repo, tool)

    found: list[Path] = []
    for depth in range(1, max_depth + 1):
        for candidate in _dirs_at_depth(repo, depth):
            if not any(candidate.is_relative_to(root) for root in found) and _build_files(candidate):
                found.append(candidate)
    if not found:
        raise ModError(f"no Maven or Gradle build found under {repo} (searched {max_depth} levels deep)")
    if len(found) > 1:
        names = ", ".join(str(p.relative_to(repo)) for p in found)
        raise ModError(f"multiple independent builds found ({names}); pick one with --build-root")
    tool = _tool_at(found[0], build_tool)
    assert tool
    return _analyze(found[0], tool)


def _dirs_at_depth(root: Path, depth: int) -> list[Path]:
    level = [root]
    for _ in range(depth):
        nxt = []
        for d in level:
            for child in sorted(d.iterdir()):
                if (child.is_dir() and not child.is_symlink() and child.name not in IGNORED_DIRS
                        and not child.name.startswith(".")):
                    nxt.append(child)
        level = nxt
    return level


def _build_files(path: Path) -> dict[str, list[str]]:
    files = {tool: [name for name in names if (path / name).is_file()] for tool, names in BUILD_FILES.items()}
    return {tool: names for tool, names in files.items() if names}


def _tool_at(path: Path, preferred: str = "auto") -> str | None:
    files = _build_files(path)
    if not files:
        return None
    if preferred != "auto":
        if preferred not in files:
            found = ", ".join(name for names in files.values() for name in names)
            raise ModError(f"no {preferred} build file at {path}; found {found}")
        return preferred
    if len(files) > 1:
        raise ModError(f"both Maven and Gradle build files found at {path}; select --build-tool maven or "
                       "--build-tool gradle. File presence cannot establish which build is active or obsolete")
    return next(iter(files))


# Imports that show a feature is in use even when the dependency arrives
# transitively (spring-boot-starter-test brings JUnit 4 and Mockito without
# either appearing in any pom). javax is limited to the Java EE packages that
# moved to jakarta.* -- javax.crypto, javax.sql, etc. stay in the JDK.
IMPORT_MARKERS = {
    "junit4": ("org.junit.",),
    "junit5": ("org.junit.jupiter.",),
    "mockito": ("org.mockito.",),
    "lombok": ("lombok.",),
    "mapstruct": ("org.mapstruct.",),
    "guava": ("com.google.common.",),
    "javax": tuple(f"javax.{name}." for name in (
        "servlet", "persistence", "validation", "inject", "ws.rs", "xml.bind", "mail", "transaction", "jms",
        "faces", "ejb", "enterprise", "websocket", "json", "activation", "batch", "security.enterprise",
    )) + ("javax.annotation.PostConstruct", "javax.annotation.PreDestroy", "javax.annotation.Resource"),
}
_IMPORT_RE = re.compile(r"^import\s+(?:static\s+)?([\w.]+)", re.MULTILINE)


def _module_dirs(path: Path, tool: str) -> list[Path]:
    """The build root plus every module/subproject directory under it.

    Maven follows <modules> recursively (a stray pom.xml under src/test is a
    test fixture, not a module). Gradle subprojects each have their own
    build file, so any build.gradle[.kts] outside src/ and output dirs counts.
    """
    if tool == "gradle":
        dirs = []
        for directory, children, files in os.walk(path):
            children[:] = sorted(name for name in children if name not in IGNORED_DIRS | {"src"}
                                 and not name.startswith("."))
            if Path(directory) == path or {"build.gradle", "build.gradle.kts"} & set(files):
                dirs.append(Path(directory))
        return dirs
    dirs, seen, queue = [], set(), deque([path.resolve()])
    while queue:
        current = queue.popleft()
        if current in seen or not (current / "pom.xml").is_file():
            continue
        seen.add(current)
        dirs.append(current)
        try:
            root = ET.parse(current / "pom.xml").getroot()
        except ET.ParseError:
            continue
        queue.extend((current / (m.text or "").strip()).resolve() for el in root if _local(el.tag) == "modules"
                     for m in el if _local(m.tag) == "module" and (m.text or "").strip())
    return dirs


def java_sources(path: Path) -> Iterator[Path]:
    """Yield source paths in a stable order, pruning generated/hidden trees."""
    root = path.resolve()
    for directory, children, files in os.walk(path):
        children[:] = sorted(name for name in children if name not in IGNORED_DIRS and not name.startswith("."))
        for name in sorted(files):
            source = Path(directory) / name
            if name.endswith(".java") and source.is_file() and source.resolve().is_relative_to(root):
                yield source


def _import_features(dirs: list[Path]) -> set[str]:
    found: set[str] = set()
    for directory in dirs:
        for source in java_sources(directory / "src"):
            for name in _IMPORT_RE.findall(source.read_text(encoding="utf-8", errors="ignore")):
                for feature, prefixes in IMPORT_MARKERS.items():
                    if name.startswith(prefixes) and not (feature == "junit4" and name.startswith(
                            ("org.junit.jupiter.", "org.junit.platform."))):
                        found.add(feature)
    return found


def _analyze(path: Path, tool: str) -> BuildRoot:
    build = BuildRoot(path=path, tool=tool)
    alternatives = [name for other, names in _build_files(path).items() if other != tool for name in names]
    if alternatives:
        build.warnings.append(f"using {tool}; also found {', '.join(alternatives)}. "
                              "Check CI and build scripts before treating alternate build files as obsolete")
    dirs = _module_dirs(path, tool)
    read_deps = _maven_dependencies if tool == "maven" else _gradle_dependencies
    root_properties = _maven_properties(path) if tool == "maven" else {}
    for directory in dirs:
        build.dependencies += (read_deps(directory, root_properties) if tool == "maven" else read_deps(directory))
    read_java = _maven_java_version if tool == "maven" else _gradle_java_version
    build.current_java = next((v for v in map(read_java, dirs) if v), None)
    for feature, (group_prefix, artifact) in FEATURE_MARKERS.items():
        for dep in build.dependencies:
            if dep.group.startswith(group_prefix) and (artifact is None or dep.artifact == artifact):
                build.features.add(feature)
                break
    build.features |= _import_features(dirs)
    # Most Spring Boot projects inherit their version from a parent POM or a
    # Gradle plugin block rather than declaring it on each starter dependency
    # (which is then version-less, inherited from Boot's BOM) -- check those
    # first, and only fall back to an explicitly-versioned dependency.
    read_boot = _maven_parent_boot_version if tool == "maven" else _gradle_boot_plugin_version
    boot = next((v for v in map(read_boot, dirs) if v), None)
    if not boot:
        boot = next((d.version for d in build.dependencies if d.group == "org.springframework.boot" and d.version), None)
    if boot:
        build.features.add("spring-boot")
    build.spring_boot = boot or None
    return build


def _local(tag: str) -> str:
    """Strip an XML namespace, e.g. ``{http://maven...}project`` -> ``project``."""
    return tag.rsplit("}", 1)[-1]


def _maven_parent_boot_version(path: Path) -> str | None:
    try:
        root = ET.parse(path / "pom.xml").getroot()
    except ET.ParseError:
        return None
    for child in root:
        if _local(child.tag) != "parent":
            continue
        fields = {_local(c.tag): (c.text or "").strip() for c in child}
        if fields.get("groupId") == "org.springframework.boot" and fields.get("artifactId") == "spring-boot-starter-parent":
            return fields.get("version") or None
    return None


_GRADLE_BOOT_PLUGIN_RE = re.compile(
    r"""id\s*\(?\s*['"]org\.springframework\.boot['"]\s*\)?\s+version\s*\(?\s*['"]([\w.\-]+)['"]"""
)


def _gradle_boot_plugin_version(path: Path) -> str | None:
    for name in ("build.gradle", "build.gradle.kts"):
        file = path / name
        if file.is_file():
            match = _GRADLE_BOOT_PLUGIN_RE.search(file.read_text(encoding="utf-8", errors="ignore"))
            if match:
                return match.group(1)
    return None


def _parse_pom(path: Path) -> ET.Element:
    try:
        return ET.parse(path / "pom.xml").getroot()
    except ET.ParseError as exc:
        raise ModError(f"could not parse {path / 'pom.xml'}: {exc}") from exc


def _maven_properties(path: Path) -> dict[str, str]:
    return {
        _local(child.tag): (child.text or "").strip()
        for parent in _parse_pom(path) if _local(parent.tag) == "properties"
        for child in parent
    }


def _maven_dependencies(path: Path, inherited: dict[str, str] | None = None) -> list[Dependency]:
    """*path*'s own <dependencies>, resolving ${...} against its properties, then *inherited* ones."""
    root = _parse_pom(path)
    properties = (inherited or {}) | _maven_properties(path)

    def resolve(value: str | None) -> str:
        if not value:
            return ""
        match = re.fullmatch(r"\$\{([^}]+)}", value.strip())
        return properties.get(match.group(1), value) if match else value

    deps: list[Dependency] = []
    for parent in root:
        if _local(parent.tag) not in {"dependencies"}:
            continue
        for dep in parent:
            if _local(dep.tag) != "dependency":
                continue
            fields = {_local(c.tag): (c.text or "").strip() for c in dep}
            if "groupId" in fields and "artifactId" in fields:
                deps.append(Dependency(fields["groupId"], fields["artifactId"], resolve(fields.get("version", ""))))
    return deps


def _maven_java_version(path: Path) -> int | None:
    text = (path / "pom.xml").read_text(encoding="utf-8", errors="ignore")
    # "(?:1\.)?" strips the legacy "1." of 1.8 -- not "1?\.?", which turned 11 into 1 and 17 into 7.
    for pattern in (r"<maven\.compiler\.release>\s*(\d+)", r"<release>\s*(\d+)",
                    r"<maven\.compiler\.source>\s*(?:1\.)?(\d+)", r"<java\.version>\s*(?:1\.)?(\d+)"):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


_GRADLE_DEP_RE = re.compile(
    r"""(?:implementation|api|compile(?:Only)?|testImplementation|testCompile|runtimeOnly)\s*[(\s]+
        ['"]([\w.\-]+):([\w.\-]+):([\w.\-\[\],+]+)['"]""",
    re.VERBOSE,
)


def _gradle_dependencies(path: Path) -> list[Dependency]:
    deps: list[Dependency] = []
    for name in ("build.gradle", "build.gradle.kts"):
        file = path / name
        if file.is_file():
            text = file.read_text(encoding="utf-8", errors="ignore")
            deps.extend(Dependency(g, a, v) for g, a, v in _GRADLE_DEP_RE.findall(text))
    return deps


def _gradle_java_version(path: Path) -> int | None:
    text = ""
    for name in ("build.gradle", "build.gradle.kts"):
        file = path / name
        if file.is_file():
            text += file.read_text(encoding="utf-8", errors="ignore")
    for pattern in (r"languageVersion\.set\(JavaLanguageVersion\.of\((\d+)\)\)",
                    r"JavaVersion\.VERSION_(?:1_)?(\d+)", r"sourceCompatibility\s*=\s*['\"]?(?:1\.)?(\d+)"):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None
