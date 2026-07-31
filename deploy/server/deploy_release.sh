#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi

repo_source=${1:-https://github.com/lostmyukii/8.git}
source_ref=${2:-main}
release_stamp=$(date -u +%Y%m%dT%H%M%SZ)
release_dir="/srv/maze/releases/${release_stamp}"
candidate_link="/srv/maze/.current-${release_stamp}"
current_link="/srv/maze/current"
previous_link="/srv/maze/previous"
previous_target=""

if ! id maze >/dev/null 2>&1; then
  echo "Runtime user 'maze' is missing. Run install_host.sh first." >&2
  exit 1
fi

install -d -m 0755 -o maze -g maze /srv/maze/releases /srv/maze/logs
if [[ -d ${repo_source} ]]; then
  install -d -m 0755 "${release_dir}"
  cp -a "${repo_source}/." "${release_dir}/"
else
  clone_ok=false
  for clone_attempt in 1 2 3; do
    if git -c http.version=HTTP/1.1 clone \
      --filter=blob:none \
      --no-checkout \
      "${repo_source}" \
      "${release_dir}"; then
      clone_ok=true
      break
    fi
    case ${release_dir} in
      /srv/maze/releases/*) rm -rf -- "${release_dir}" ;;
      *)
        echo "Refusing to clean unexpected release path: ${release_dir}" >&2
        exit 1
        ;;
    esac
    echo "Git clone attempt ${clone_attempt} failed; retrying." >&2
    sleep 3
  done
  if [[ ${clone_ok} != true ]]; then
    echo "Unable to clone ${repo_source} after 3 attempts." >&2
    exit 1
  fi
  git -C "${release_dir}" checkout --detach "${source_ref}"
fi
chown -R maze:maze "${release_dir}"

if [[ ! -r ${release_dir}/requirements.txt ]]; then
  echo "Release source is missing requirements.txt: ${release_dir}" >&2
  exit 1
fi

sudo -u maze python3 -m venv "${release_dir}/.venv"
sudo -u maze "${release_dir}/.venv/bin/python" -m pip install --upgrade pip
sudo -u maze "${release_dir}/.venv/bin/python" -m pip install -r "${release_dir}/requirements.txt"
sudo -u maze "${release_dir}/.venv/bin/python" -m compileall \
  "${release_dir}/rdk_maze_tuner" \
  "${release_dir}/simulation"

if [[ -L ${current_link} ]]; then
  previous_target=$(readlink -f "${current_link}")
  ln -sfn "${previous_target}" "${previous_link}"
fi
ln -s "${release_dir}" "${candidate_link}"
mv -Tf "${candidate_link}" "${current_link}"

install -m 0755 "${current_link}/deploy/server/maze-sim-mode" /usr/local/sbin/maze-sim-mode
install -m 0440 "${current_link}/deploy/server/maze-sim-mode.sudoers" /etc/sudoers.d/maze-sim-mode
visudo -cf /etc/sudoers.d/maze-sim-mode
install -m 0755 -o maze -g maze "${current_link}/deploy/server/vnc/xstartup" /home/maze/.vnc/xstartup

for unit in \
  maze-dashboard.service \
  maze-novnc.service \
  maze-vnc.service \
  maze-webots-desktop.service \
  maze-webots-headless.service \
  maze-webots-stream.service; do
  install -m 0644 "${current_link}/deploy/server/systemd/${unit}" "/etc/systemd/system/${unit}"
done

systemctl daemon-reload
systemctl enable --now maze-vnc.service maze-novnc.service
systemctl enable maze-webots-stream.service maze-dashboard.service
systemctl stop maze-dashboard.service
systemctl restart maze-webots-stream.service
systemctl start maze-dashboard.service

wait_http() {
  local url=$1
  local attempts=${2:-60}
  local index
  for ((index = 1; index <= attempts; index += 1)); do
    if curl -fsS "${url}" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

if ! wait_http http://127.0.0.1:8000/api/state 60 ||
  ! wait_http http://127.0.0.1:1234/index.html 60; then
  echo "Release health check failed." >&2
  systemctl --no-pager --full status maze-webots-stream.service maze-dashboard.service || true
  if [[ -n ${previous_target} && -d ${previous_target} ]]; then
    rollback_candidate="/srv/maze/.health-rollback-${release_stamp}"
    ln -s "${previous_target}" "${rollback_candidate}"
    mv -Tf "${rollback_candidate}" "${current_link}"
    systemctl restart maze-webots-stream.service maze-dashboard.service
    echo "Restored previous release: ${previous_target}" >&2
  fi
  exit 1
fi

echo "Release deployed: ${release_dir}"
if [[ -d ${release_dir}/.git ]]; then
  echo "Current commit: $(sudo -u maze git -C "${release_dir}" rev-parse HEAD)"
else
  echo "Current source: local archive (${source_ref})"
fi
