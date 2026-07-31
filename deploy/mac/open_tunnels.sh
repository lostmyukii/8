#!/usr/bin/env bash
set -Eeuo pipefail

ssh_host=${1:-maze-cvm}
exec ssh \
  -N \
  -L 8000:127.0.0.1:8000 \
  -L 1234:127.0.0.1:1234 \
  -L 6080:127.0.0.1:6080 \
  "${ssh_host}"
