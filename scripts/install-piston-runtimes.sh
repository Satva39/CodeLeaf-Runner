#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PISTON_SOURCE_DIR="${PISTON_SOURCE_DIR:-${ROOT_DIR}/vendor/piston}"
PISTON_ADMIN_URL="${PISTON_ADMIN_URL:-http://127.0.0.1:2000}"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required to fetch the Piston CLI." >&2
  exit 1
fi
if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required to run the Piston package manager CLI." >&2
  exit 1
fi

NODE_MAJOR="$(node -p 'Number(process.versions.node.split(".")[0])')"
if [[ "${NODE_MAJOR}" -lt 15 ]]; then
  echo "Piston's CLI requires Node.js 15 or newer; found $(node --version)." >&2
  exit 1
fi

if [[ ! -f "${PISTON_SOURCE_DIR}/cli/index.js" ]]; then
  mkdir -p "$(dirname "${PISTON_SOURCE_DIR}")"
  git clone --depth 1 https://github.com/engineer-man/piston.git "${PISTON_SOURCE_DIR}"
fi

if [[ ! -d "${PISTON_SOURCE_DIR}/cli/node_modules" ]]; then
  npm install --prefix "${PISTON_SOURCE_DIR}/cli"
fi

PISTON_CLI=(node "${PISTON_SOURCE_DIR}/cli/index.js" -u "${PISTON_ADMIN_URL}")

if [[ "${1:-}" == "--list" ]]; then
  "${PISTON_CLI[@]}" ppman list
  exit 0
fi

if [[ "$#" -eq 0 ]]; then
  cat <<'HELP'
Usage:
  ./scripts/install-piston-runtimes.sh --list
  ./scripts/install-piston-runtimes.sh python c c++ node java php go rust

First list available Piston packages, then pass the exact package names you want.
The package installer is an administrator tool and is not callable by learner code.
HELP
  exit 2
fi

for package in "$@"; do
  if [[ ! "${package}" =~ ^[a-zA-Z0-9._+-]+(=[a-zA-Z0-9.*+_-]+)?$ ]]; then
    echo "Invalid Piston package selector: ${package}" >&2
    exit 2
  fi
  echo "Installing Piston runtime package: ${package}"
  "${PISTON_CLI[@]}" ppman install "${package}"
done

printf '\nInstalled runtime catalogue:\n'
curl --fail --silent --show-error "${PISTON_ADMIN_URL}/api/v2/runtimes"
printf '\n'
