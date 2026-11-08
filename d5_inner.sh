#!/usr/bin/env bash
# Runs INSIDE a fresh WSL distro. Usage: d5_inner.sh provision | stranger <ollama-host-ip>
set -u
export PATH="/opt/venv/bin:/root/.local/bin:$PATH"
case "$1" in
provision)
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || exit 1
  apt-get install -y -qq curl ca-certificates python3 python3-venv python3-pip git >/dev/null || exit 1
  python3 -m venv /opt/venv && /opt/venv/bin/pip install -q mitmproxy cryptography aider-chat || exit 1
  echo "provisioned: $(python3 --version) $(/opt/venv/bin/mitmdump --version | head -1)"
  ;;
stranger)
  HIP=$2
  cd /root && rm -rf corral proj .mitmproxy .corral .aider* && mkdir corral && tar xf /opt/d5/corral.tar -C corral || exit 1
  cd /root/corral
  step() { echo "\$ $1"; bash -c "$1" 2>&1 | tail -${2:-8} | cut -c1-200; echo "rc=${PIPESTATUS[0]}"; }
  step 'bash install.sh ~/proj'
  step '~/proj/.claude/bin/corral status' 12
  export OLLAMA_API_BASE="http://$HIP:11434" SILENT_ALLOW_EXTRA="$HIP"
  step 'bash silent/silent.sh aider --model ollama_chat/qwen2.5-coder:7b --message "say hi" --yes-always --no-git' 6
  step 'bash silent/verify.sh attestation.json'
  # D6: guards without node; then also without python3 (sed fallback)
  step 'command -v node || echo "node: absent"'
  step 'bash tests/test_guards.sh' 10
  step 'mkdir -p /tmp/nopy; for b in bash git grep sed awk cat mktemp rm head tr cut env dirname date; do ln -sf "$(command -v $b)" /tmp/nopy/$b; done; PATH=/tmp/nopy bash tests/test_guards.sh' 10
  ;;
esac
