#!/usr/bin/env bash
# Install javamod's prerequisites on a bare Ubuntu/Debian host (24.04/26.04
# headless included) for anyone who doesn't want the devcontainer. Gradle
# is installed in .venv/bin when the system version is incompatible.
set -euo pipefail

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This script supports Ubuntu/Debian (apt-get). On another OS, install" >&2
  echo "manually: git, a JDK (default: 21) + javac, Maven or Gradle, python3." >&2
  exit 1
fi

JAVA_VERSION="${JAVAMOD_JAVA:-21}"
case "$JAVA_VERSION" in
  11|17|21|25) ;;
  *) echo "JAVAMOD_JAVA must be 11, 17, 21, or 25" >&2; exit 1 ;;
esac
missing=()
command -v git            >/dev/null 2>&1 || missing+=("git")
# Reuse the target JDK, including manual installations via JAVA_HOME.
jdk_home="${JAVA_HOME:-}"
if [ -z "$jdk_home" ] && command -v javac >/dev/null 2>&1; then
  jdk_home="$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")"
fi
jdk_version="$("$jdk_home/bin/javac" -version 2>&1 || true)"
if [[ ! "$jdk_version" =~ ^javac\ ${JAVA_VERSION}([.\ -]|$) ]]; then
  jdks=(/usr/lib/jvm/java-"${JAVA_VERSION}"-openjdk-*)
  jdk_home="${jdks[0]}"
  [ -x "$jdk_home/bin/javac" ] || missing+=("openjdk-${JAVA_VERSION}-jdk")
fi
command -v mvn            >/dev/null 2>&1 || missing+=("maven")
command -v python3        >/dev/null 2>&1 || missing+=("python3")
python3 -c "import venv"  >/dev/null 2>&1 || missing+=("python3-venv")
command -v pip3           >/dev/null 2>&1 || missing+=("python3-pip")
command -v curl           >/dev/null 2>&1 || missing+=("curl")
command -v unzip          >/dev/null 2>&1 || missing+=("unzip")
if [ "$(dpkg-query -W -f='${Status}' ca-certificates 2>/dev/null || true)" != 'install ok installed' ]; then
  missing+=("ca-certificates")
fi

if [ "${#missing[@]}" -gt 0 ]; then
  echo "Installing: ${missing[*]}"
  sudo apt-get update
  sudo apt-get install -y "${missing[@]}"
else
  echo "All system prerequisites already present."
fi

if [ ! -x "$jdk_home/bin/javac" ]; then
  # Expand again after apt installs a previously absent target JDK.
  jdks=(/usr/lib/jvm/java-"${JAVA_VERSION}"-openjdk-*)
  jdk_home="${jdks[0]}"
fi
export JAVA_HOME="$jdk_home"
if [ ! -x "$JAVA_HOME/bin/javac" ]; then
  echo "Target JDK not found at $JAVA_HOME" >&2
  exit 1
fi
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -e . -q
export PATH="$PWD/.venv/bin:$JAVA_HOME/bin:$PATH"

if ! python -m javamod.toolchain mvn --java "$JAVA_VERSION"; then
  sudo apt-get update
  sudo apt-get install -y maven
  python -m javamod.toolchain mvn --java "$JAVA_VERSION"
fi

if ! python -m javamod.toolchain gradle --java "$JAVA_VERSION"; then
  # Stay on Gradle 8 for older projects; Java 25 requires Gradle 9.1+.
  if [ "$JAVA_VERSION" -ge 25 ]; then
    gradle_version="${JAVAMOD_GRADLE_VERSION:-9.7.1}"
  else
    gradle_version="${JAVAMOD_GRADLE_VERSION:-8.14.5}"
  fi
  if [[ ! "$gradle_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "JAVAMOD_GRADLE_VERSION must be a stable version such as 8.14.5" >&2
    exit 1
  fi
  download_dir="$(mktemp -d)"
  trap 'rm -rf "$download_dir"' EXIT
  archive="gradle-${gradle_version}-bin.zip"
  url="https://services.gradle.org/distributions/$archive"
  echo "Installing Gradle $gradle_version in .venv (system Gradle is preserved)."
  curl --fail --location --retry 3 "$url" -o "$download_dir/$archive"
  curl --fail --location --retry 3 "$url.sha256" -o "$download_dir/checksum"
  checksum="$(cat "$download_dir/checksum")"
  if [[ ! "$checksum" =~ ^[a-fA-F0-9]{64}$ ]]; then
    echo "Invalid Gradle distribution checksum" >&2
    exit 1
  fi
  printf '%s  %s\n' "$checksum" "$download_dir/$archive" | sha256sum --check
  mkdir -p .venv/tools
  unzip -q -o "$download_dir/$archive" -d .venv/tools
  ln -sfn "../tools/gradle-${gradle_version}/bin/gradle" .venv/bin/gradle
  python -m javamod.toolchain gradle --java "$JAVA_VERSION"
fi
.venv/bin/javamod doctor --java "$JAVA_VERSION"
echo
echo "Done. Activate with: source .venv/bin/activate"
printf 'Use the target JDK in each terminal: export JAVA_HOME=%q\n' "$JAVA_HOME"
echo 'export PATH="$JAVA_HOME/bin:$PATH"'
echo "Then run:            javamod doctor"
