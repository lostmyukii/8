#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root on the RDK X3." >&2
  exit 1
fi

SOURCE_DIR="${1:-}"
if [[ -z "${SOURCE_DIR}" || ! -f "${SOURCE_DIR}/requirements.txt" ]]; then
  echo "Usage: sudo ./install_agent.sh /path/to/project-source" >&2
  exit 1
fi

install -d -m 0755 /opt/maze-agent
install -d -m 0750 /var/lib/maze-agent
install -d -m 0750 /etc/maze-agent

if ! id maze-agent >/dev/null 2>&1; then
  useradd --system --home /var/lib/maze-agent --shell /usr/sbin/nologin maze-agent
fi
usermod -a -G dialout maze-agent

python3 -m venv /opt/maze-agent/venv
/opt/maze-agent/venv/bin/pip install --upgrade pip
/opt/maze-agent/venv/bin/pip install -r "${SOURCE_DIR}/requirements.txt"

rm -f /opt/maze-agent/current
ln -s "${SOURCE_DIR}" /opt/maze-agent/current
install -m 0644 \
  "${SOURCE_DIR}/deploy/rdk/maze-agent.service" \
  /etc/systemd/system/maze-agent.service

if [[ ! -f /etc/maze-agent/maze-agent.env ]]; then
  install -m 0600 \
    "${SOURCE_DIR}/deploy/rdk/maze-agent.env.example" \
    /etc/maze-agent/maze-agent.env
fi

chown -R maze-agent:maze-agent /var/lib/maze-agent
chown root:maze-agent /etc/maze-agent/maze-agent.env
chmod 0600 /etc/maze-agent/maze-agent.env

systemctl daemon-reload
systemctl enable maze-agent.service

echo "Edit /etc/maze-agent/maze-agent.env, then run:"
echo "  sudo systemctl restart maze-agent.service"
