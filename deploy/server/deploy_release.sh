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
acceptance_root="/srv/maze/shared/acceptance/physical"
public_url=${MAZE_PUBLIC_URL:-https://8.ilelezhan.cn/}
previous_target=""
switched=false
active_sim_services=()

sim_services=(
  maze-webots-stream.service
  maze-webots-desktop.service
  maze-webots-headless.service
)

fail_restore() {
  local code=$?
  trap - ERR
  if [[ ${switched} == true && -n ${previous_target} && -d ${previous_target} ]]; then
    local rollback_link="/srv/maze/.deploy-rollback-${release_stamp}"
    ln -s "${previous_target}" "${rollback_link}"
    mv -Tf "${rollback_link}" "${current_link}"
    systemctl daemon-reload
    systemctl restart maze-webots-stream.service maze-dashboard.service || true
    echo "Deployment failed; restored ${previous_target}" >&2
  elif ((${#active_sim_services[@]} > 0)); then
    systemctl start "${active_sim_services[@]}" || true
    systemctl restart maze-dashboard.service || true
    echo "Candidate acceptance failed; prior simulation restored." >&2
  fi
  exit "${code}"
}
trap fail_restore ERR

if ! id maze >/dev/null 2>&1; then
  echo "Runtime user 'maze' is missing. Run install_host.sh first." >&2
  exit 1
fi
if [[ -e ${release_dir} ]]; then
  echo "Release path already exists: ${release_dir}" >&2
  exit 1
fi

install -d -m 0755 -o maze -g maze \
  /srv/maze/releases \
  /srv/maze/logs \
  /srv/maze/shared
install -d -m 0755 -o maze -g maze "${acceptance_root}"

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
      /srv/maze/releases/*)
        rm -rf -- "${release_dir}"
        ;;
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

for required in requirements.txt requirements-dev.txt; do
  if [[ ! -r ${release_dir}/${required} ]]; then
    echo "Release source is missing ${required}: ${release_dir}" >&2
    exit 1
  fi
done

sudo -u maze python3 -m venv "${release_dir}/.venv"
sudo -u maze "${release_dir}/.venv/bin/python" -m pip install --upgrade pip
sudo -u maze "${release_dir}/.venv/bin/python" -m pip install \
  -r "${release_dir}/requirements-dev.txt"

sudo -u maze env PYTHONDONTWRITEBYTECODE=1 \
  "${release_dir}/.venv/bin/python" -m compileall -q \
  "${release_dir}/rdk_maze_tuner" \
  "${release_dir}/simulation"
sudo -u maze env PYTHONDONTWRITEBYTECODE=1 \
  "${release_dir}/.venv/bin/python" -m pytest \
  "${release_dir}/rdk_maze_tuner/tests" -q
sudo -u maze "${release_dir}/.venv/bin/pio" run \
  -d "${release_dir}/esp32_firmware"
while IFS= read -r javascript; do
  node --check "${javascript}"
done < <(find "${release_dir}/rdk_maze_tuner/dashboard/static" \
  -maxdepth 1 -type f -name '*.js' -print | sort)

if [[ -L ${current_link} ]]; then
  previous_target=$(readlink -f "${current_link}")
fi
for service in "${sim_services[@]}"; do
  if systemctl is-active --quiet "${service}"; then
    active_sim_services+=("${service}")
  fi
done
if ((${#active_sim_services[@]} > 0)); then
  systemctl stop "${active_sim_services[@]}"
fi

sudo -u maze env \
  HOME=/home/maze \
  PYTHONUNBUFFERED=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  MAZE_PHYSICAL_PROFILE_DIR="${release_dir}/simulation/webots/maze_car/config/physical_profiles" \
  "${release_dir}/.venv/bin/python" \
  -m simulation.webots.maze_car.tools.run_physical_acceptance \
  --webots /usr/local/bin/webots \
  --world "${release_dir}/simulation/webots/maze_car/worlds/maze_physical_calibration.wbt" \
  --scenarios "${release_dir}/simulation/webots/maze_car/config/acceptance_scenarios.yaml" \
  --output "${acceptance_root}"

chmod -R a-w "${release_dir}"
if [[ -n ${previous_target} && -d ${previous_target} ]]; then
  ln -sfn "${previous_target}" "${previous_link}"
fi
ln -s "${release_dir}" "${candidate_link}"
mv -Tf "${candidate_link}" "${current_link}"
switched=true

install -m 0755 \
  "${current_link}/deploy/server/maze-sim-mode" \
  /usr/local/sbin/maze-sim-mode
install -m 0440 \
  "${current_link}/deploy/server/maze-sim-mode.sudoers" \
  /etc/sudoers.d/maze-sim-mode
visudo -cf /etc/sudoers.d/maze-sim-mode
install -m 0755 -o maze -g maze \
  "${current_link}/deploy/server/vnc/xstartup" \
  /home/maze/.vnc/xstartup

for unit in \
  maze-dashboard.service \
  maze-novnc.service \
  maze-vnc.service \
  maze-webots-desktop.service \
  maze-webots-headless.service \
  maze-webots-stream.service; do
  install -m 0644 \
    "${current_link}/deploy/server/systemd/${unit}" \
    "/etc/systemd/system/${unit}"
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
    if curl -fsS --connect-timeout 2 --max-time 5 "${url}" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_tcp() {
  local host=$1
  local port=$2
  local attempts=${3:-60}
  local index
  for ((index = 1; index <= attempts; index += 1)); do
    if timeout 2 bash -c "</dev/tcp/${host}/${port}" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

require_loopback_listener() {
  local port=$1
  local listeners
  listeners=$(ss -ltnH "sport = :${port}")
  [[ -n ${listeners} ]] || return 1
  while IFS= read -r listener; do
    local address
    address=$(awk '{print $4}' <<<"${listener}")
    case "${address}" in
      127.0.0.1:"${port}"|\[::1\]:"${port}")
        ;;
      *)
        echo "Port ${port} is not loopback-only: ${address}" >&2
        return 1
        ;;
    esac
  done <<<"${listeners}"
}

if ! wait_http http://127.0.0.1:8000/ 60 ||
  ! wait_http http://127.0.0.1:1234/index.html 60 ||
  ! wait_tcp 127.0.0.1 8765 60 ||
  ! require_loopback_listener 8765 ||
  ! wait_http "${public_url}" 30; then
  systemctl --no-pager --full status \
    maze-webots-stream.service \
    maze-dashboard.service || true
  exit 1
fi

trap - ERR
echo "Release deployed: ${release_dir}"
if [[ -d ${release_dir}/.git ]]; then
  echo "Current commit: $(git -C "${release_dir}" rev-parse HEAD)"
else
  echo "Current source: local archive (${source_ref})"
fi
echo "Physical acceptance: ${acceptance_root}"
