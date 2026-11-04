#!/usr/bin/env bash
set -euo pipefail

echo "== OS =="
cat /etc/os-release || true

echo
echo "== CPU architecture =="
arch="$(uname -m)"
echo "uname -m: ${arch}"
if [[ "${arch}" != "aarch64" && "${arch}" != "arm64" ]]; then
  echo "FAIL: This host is not ARM64/AArch64."
  exit 1
fi

echo
echo "== cgroup v2 =="
if [[ ! -f /sys/fs/cgroup/cgroup.controllers ]]; then
  echo "FAIL: cgroup v2 controllers file is missing."
  exit 1
fi
if ! mount | grep -q 'type cgroup2'; then
  echo "FAIL: cgroup2 is not mounted."
  exit 1
fi
cat /sys/fs/cgroup/cgroup.controllers

echo
echo "== Docker =="
docker info --format 'OSType={{.OSType}} Architecture={{.Architecture}} CgroupVersion={{.CgroupVersion}} CgroupDriver={{.CgroupDriver}}'
docker version
docker compose version

docker_arch="$(docker info --format '{{.Architecture}}')"
case "${docker_arch}" in
  aarch64|arm64) ;;
  *) echo "FAIL: Docker reports architecture '${docker_arch}', expected aarch64/arm64."; exit 1 ;;
esac

cgroup_version="$(docker info --format '{{.CgroupVersion}}')"
if [[ "${cgroup_version}" != "2" ]]; then
  echo "FAIL: Docker daemon must use cgroup v2."
  exit 1
fi

echo
echo "PASS: native ARM64 Linux + cgroup v2 + Docker are available."
echo "This preflight does not certify Isolate until the Piston execution tests pass on this host."
