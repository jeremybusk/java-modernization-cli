"""Version checks shared by doctor and the Ubuntu/Debian bootstrap."""
from __future__ import annotations

import os
import re
import shutil
import subprocess


MAVEN_MIN = (3, 6, 1)  # --no-transfer-progress was added in 3.6.1.
# Minimum Gradle for running on each JVM; see Gradle's compatibility matrix.
GRADLE_JAVA_MIN = {
    8: (4, 0), 9: (4, 3), 10: (4, 7), 11: (5, 0), 12: (5, 4),
    13: (6, 0), 14: (6, 3), 15: (6, 7), 16: (7, 0), 17: (7, 3),
    18: (7, 5), 19: (7, 6), 20: (8, 3), 21: (8, 5), 22: (8, 8),
    23: (8, 10), 24: (8, 14), 25: (9, 1), 26: (9, 4), 27: (9, 8),
}


def probe(name: str) -> tuple[tuple[int, ...] | None, str]:
    """Read a tool version without running a project or downloading plugins."""
    binary = name
    if name in ("java", "javac") and os.environ.get("JAVA_HOME"):
        binary = os.path.join(os.environ["JAVA_HOME"], "bin", name)
    path = shutil.which(binary)
    if not path:
        return None, f"{binary} is not installed or not on PATH"
    try:
        result = subprocess.run([path, "-version" if name in ("java", "javac") else "--version"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"cannot query {name}: {exc}"
    text = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    patterns = {"java": r'(?:java|openjdk) (?:version )?"?(\d+(?:\.\d+)*)',
                "javac": r"javac (\d+(?:\.\d+)*)", "mvn": r"Apache Maven (\d+(?:\.\d+)*)",
                "gradle": r"(?m)^Gradle (\d+(?:\.\d+)*)"}
    match = re.search(patterns[name], text)
    if result.returncode or not match:
        return None, f"{name} version check failed ({path}); run {name} --version for details"
    version = tuple(map(int, match[1].split(".")))
    if name in ("java", "javac") and version[0] == 1:
        version = version[1:]  # Java 8 reports 1.8.
    return version, f"{match[1]} ({path})"


def requirement(name: str, version: tuple[int, ...], *, java: int, runtime: int) -> str | None:
    if name in ("java", "javac"):
        return f"requires JDK {java}+" if version[0] < java else None
    if name == "mvn":
        return "requires Maven 3.6.1+" if version < MAVEN_MIN else None
    # A newer JVM can need a newer Gradle even when targeting older Java.
    needed_java = max(java, runtime)
    minimum = GRADLE_JAVA_MIN.get(needed_java)
    if minimum is None:
        return f"Gradle compatibility is unknown for Java {needed_java}"
    if version < minimum:
        return f"requires Gradle {'.'.join(map(str, minimum))}+ for Java {needed_java}"
    if version[0] >= 9 and runtime < 17:
        return "Gradle 9+ requires a runtime JDK of at least 17"
    return None


def check(name: str, *, java: int, runtime: int) -> tuple[bool, str]:
    version, detail = probe(name)
    if version is None:
        return False, detail
    problem = requirement(name, version, java=java, runtime=runtime)
    return problem is None, detail + (f"; {problem}" if problem else "")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=("mvn", "gradle"))
    parser.add_argument("--java", type=int, required=True)
    args = parser.parse_args()
    runtime, detail = probe("java")
    if runtime is None:
        parser.exit(1, detail + "\n")
    ok, detail = check(args.tool, java=args.java, runtime=runtime[0])
    print(detail)
    raise SystemExit(0 if ok else 1)
