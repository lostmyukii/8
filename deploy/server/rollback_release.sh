#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi

if [[ ! -L /srv/maze/previous ]]; then
  echo "No previous release is available." >&2
  exit 1
fi

previous_target=$(readlink -f /srv/maze/previous)
if [[ ! -d ${previous_target} ]]; then
  echo "Previous release directory is missing: ${previous_target}" >&2
  exit 1
fi

candidate_link="/srv/maze/.rollback-$(date -u +%Y%m%dT%H%M%SZ)"
ln -s "${previous_target}" "${candidate_link}"
mv -Tf "${candidate_link}" /srv/maze/current
systemctl restart maze-webots-stream.service maze-dashboard.service

echo "Rolled back to ${previous_target}"
