#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi

release_id=${1:-}
if [[ -z ${release_id} ]]; then
  if [[ ! -L /srv/maze/previous ]]; then
    echo "Usage: rollback_release.sh RELEASE_ID" >&2
    echo "No previous release is available." >&2
    exit 1
  fi
  release_id=$(basename "$(readlink -f /srv/maze/previous)")
fi
if [[ ! ${release_id} =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Invalid release ID: ${release_id}" >&2
  exit 1
fi

target="/srv/maze/releases/${release_id}"
if [[ ! -d ${target} ]]; then
  echo "Release directory is missing: ${target}" >&2
  exit 1
fi
for required in \
  deploy/server/systemd/maze-dashboard.service \
  deploy/server/systemd/maze-webots-stream.service \
  deploy/server/maze-sim-mode; do
  if [[ ! -r ${target}/${required} ]]; then
    echo "Release is incomplete: ${target}/${required}" >&2
    exit 1
  fi
done

current_target=""
if [[ -L /srv/maze/current ]]; then
  current_target=$(readlink -f /srv/maze/current)
fi
candidate_link="/srv/maze/.rollback-$(date -u +%Y%m%dT%H%M%SZ)"
ln -s "${target}" "${candidate_link}"
mv -Tf "${candidate_link}" /srv/maze/current

install -m 0755 \
  /srv/maze/current/deploy/server/maze-sim-mode \
  /usr/local/sbin/maze-sim-mode
for unit in \
  maze-dashboard.service \
  maze-novnc.service \
  maze-vnc.service \
  maze-webots-desktop.service \
  maze-webots-headless.service \
  maze-webots-stream.service; do
  install -m 0644 \
    "/srv/maze/current/deploy/server/systemd/${unit}" \
    "/etc/systemd/system/${unit}"
done
systemctl daemon-reload

if ! systemctl restart \
  maze-webots-stream.service \
  maze-dashboard.service; then
  if [[ -n ${current_target} && -d ${current_target} ]]; then
    restore_link="/srv/maze/.rollback-restore-$(date -u +%Y%m%dT%H%M%SZ)"
    ln -s "${current_target}" "${restore_link}"
    mv -Tf "${restore_link}" /srv/maze/current
    systemctl daemon-reload
    systemctl restart maze-webots-stream.service maze-dashboard.service
  fi
  exit 1
fi

if [[ -n ${current_target} && -d ${current_target} ]]; then
  ln -sfn "${current_target}" /srv/maze/previous
fi
echo "Rolled back to ${target}"
