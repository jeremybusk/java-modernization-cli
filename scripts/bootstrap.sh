#!/usr/bin/env bash
# Install javamod's prerequisites on a bare Ubuntu/Debian host (24.04/26.04
# headless included) for anyone who doesn't want the devcontainer. Safe to
# re-run; only installs what's missing.
set -euo pipefail

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This script supports Ubuntu/Debian (apt-get). On another OS, install" >&2
  echo "manually: git, a JDK (default: 21) + javac, Maven or Gradle, python3." >&2
  exit 1
fi

JAVA_VERSION="${JAVAMOD_JAVA:-21}"
missing=()
command -v git            >/dev/null 2>&1 || missing+=("git")
command -v javac          >/dev/null 2>&1 || missing+=("openjdk-${JAVA_VERSION}-jdk")
command -v mvn            >/dev/null 2>&1 || missing+=("maven")
command -v gradle         >/dev/null 2>&1 || missing+=("gradle")
command -v python3        >/dev/null 2>&1 || missing+=("python3")
python3 -c "import venv"  >/dev/null 2>&1 || missing+=("python3-venv")
command -v pip3           >/dev/null 2>&1 || missing+=("python3-pip")

if [ "${#missing[@]}" -gt 0 ]; then
  echo "Installing: ${missing[*]}"
  sudo apt-get update
  sudo apt-get install -y "${missing[@]}"
else
  echo "All system prerequisites already present."
fi

cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -e . -q
echo
echo "Done. Activate with: source .venv/bin/activate"
echo "Then run:            javamod doctor"
