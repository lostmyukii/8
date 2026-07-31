#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi

source /etc/os-release
if [[ ${ID} != "ubuntu" || ${VERSION_ID} != "24.04" ]]; then
  echo "This installer requires Ubuntu 24.04." >&2
  exit 1
fi
if [[ $(dpkg --print-architecture) != "amd64" ]]; then
  echo "This installer requires amd64 because Webots R2025a is pinned to amd64." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y \
  ca-certificates \
  curl \
  dbus-x11 \
  git \
  gnupg \
  jq \
  locales \
  mesa-utils \
  novnc \
  python3-pip \
  python3-venv \
  software-properties-common \
  tigervnc-standalone-server \
  ufw \
  wget \
  websockify \
  xfce4 \
  xfce4-goodies \
  xvfb

locale-gen en_US en_US.UTF-8
update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
add-apt-repository -y universe

ros_apt_source_version=$(
  curl \
    -fsSL \
    --retry 5 \
    --retry-all-errors \
    --connect-timeout 20 \
    --max-time 120 \
    https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest |
    jq -er '.tag_name'
)
ros_apt_source_deb="/var/tmp/ros2-apt-source_${ros_apt_source_version}.noble_all.deb"
curl -fL --retry 3 \
  -o "${ros_apt_source_deb}" \
  "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${ros_apt_source_version}/ros2-apt-source_${ros_apt_source_version}.noble_all.deb"
dpkg -i "${ros_apt_source_deb}"

apt-get update
apt-get install -y ros-jazzy-desktop ros-dev-tools

webots_deb="/var/tmp/webots_2025a_amd64.deb"
webots_url="https://github.com/cyberbotics/webots/releases/download/R2025a/webots_2025a_amd64.deb"
webots_accelerated_url="https://gh-proxy.com/${webots_url}"
webots_sha256="6253d58c9b625a83ed7b62cd85a640fd0542d441c48d633a60932208b40b0657"
if ! command -v webots >/dev/null 2>&1 || ! webots --version 2>&1 | grep -q "R2025a"; then
  webots_download_ok=false
  for candidate_url in "${webots_accelerated_url}" "${webots_url}"; do
    if wget \
      --continue \
      --tries=20 \
      --timeout=60 \
      --output-document="${webots_deb}" \
      "${candidate_url}" &&
      printf '%s  %s\n' "${webots_sha256}" "${webots_deb}" | sha256sum --check -; then
      webots_download_ok=true
      break
    fi
    rm -f "${webots_deb}"
  done
  if [[ ${webots_download_ok} != true ]]; then
    echo "Webots download or checksum verification failed." >&2
    exit 1
  fi
  apt-get install -y "${webots_deb}"
fi
install -d -m 1777 -o root -g root /tmp/webots

if ! id maze >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash maze
fi
install -d -m 0755 -o maze -g maze /srv/maze
install -d -m 0755 -o maze -g maze /srv/maze/releases
install -d -m 0755 -o maze -g maze /srv/maze/logs
install -d -m 0700 -o maze -g maze /home/maze/.ssh
install -d -m 0700 -o maze -g maze /home/maze/.vnc

if [[ -s /home/ubuntu/.ssh/authorized_keys ]]; then
  install -m 0600 -o maze -g maze /home/ubuntu/.ssh/authorized_keys /home/maze/.ssh/authorized_keys
fi

ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

echo "Host installation complete."
echo "ROS: /opt/ros/jazzy/setup.bash"
echo "Webots: $(command -v webots)"
