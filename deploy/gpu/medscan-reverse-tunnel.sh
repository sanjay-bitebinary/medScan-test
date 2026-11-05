#!/usr/bin/env bash
set -euo pipefail

PUBLIC_HOST="${MEDSCAN_PUBLIC_HOST:?Set MEDSCAN_PUBLIC_HOST}"
KEY="${MEDSCAN_TUNNEL_KEY:-$HOME/.ssh/medscan_public_tunnel_ed25519}"

exec ssh -N \
  -i "$KEY" \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:18003:127.0.0.1:8003 \
  "root@$PUBLIC_HOST"
