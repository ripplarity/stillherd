#!/usr/bin/env bash
# Runs INSIDE the clean WSL distro. Usage: inner.sh provision | run <opencode|aider|goose> <1|2> <ollama-host-ip>
set -u
export PATH="/root/.local/bin:$PATH"
case "$1" in
provision)
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || exit 1
  apt-get install -y -qq curl ca-certificates python3 python3-venv python3-pip nodejs npm libxcb1 libgomp1 bzip2 >/dev/null || exit 1
  python3 -m venv /opt/mitm && /opt/mitm/bin/pip install -q mitmproxy || exit 1
  python3 -m venv /opt/aider && /opt/aider/bin/pip install -q aider-chat || echo "aider install failed" >&2
  npm install -g opencode-ai@latest >/dev/null 2>&1 || echo "opencode install failed" >&2
  curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh | CONFIGURE=false bash >/dev/null 2>&1 || echo "goose install failed" >&2
  /opt/mitm/bin/mitmdump -q --listen-port 8099 & p=$!; sleep 8; kill $p 2>/dev/null
  cp /root/.mitmproxy/mitmproxy-ca-cert.pem /usr/local/share/ca-certificates/mitm.crt
  update-ca-certificates >/dev/null 2>&1
  echo "mitmproxy=$(/opt/mitm/bin/mitmdump --version 2>/dev/null | head -1)"
  echo "opencode=$(opencode --version 2>/dev/null | tail -1) aider=$(/opt/aider/bin/aider --version 2>/dev/null | tail -1) goose=$(goose --version 2>/dev/null | tail -1)"
  ;;
run)
  tool=$2; n=$3; HIP=$4
  rm -rf /root/.config/opencode /root/.local/share/opencode /root/.cache/opencode /root/.aider* /root/.cache/aider \
         /root/.config/goose /root/.local/share/goose /root/.local/state/goose /tmp/w /tmp/s_${tool}_${n}_*
  mkdir -p /tmp/w && cd /tmp/w
  printf 'def add(a, b):\n    return a + b\n' > calc.py
  printf 'from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n' > test_calc.py
  case "$tool" in
    opencode) VER=$(opencode --version 2>/dev/null | tail -1) ;;
    aider) VER=$(/opt/aider/bin/aider --version 2>/dev/null | tail -1 | awk '{print $NF}') ;;
    goose) VER=$(goose --version 2>/dev/null | tail -1 | awk '{print $NF}') ;;
  esac
  export EGRESS_TOOL=$tool EGRESS_VERSION=$VER EGRESS_OUT=/tmp/res_${tool}_${n}.jsonl
  : > "$EGRESS_OUT"
  /opt/mitm/bin/mitmdump --listen-host 127.0.0.1 --listen-port 8080 -q -s /opt/stillherd/log_requests.py >/tmp/mitm.log 2>&1 &
  MP=$!
  for i in $(seq 1 30); do (echo > /dev/tcp/127.0.0.1/8080) 2>/dev/null && break; sleep 1; done
  export HTTP_PROXY=http://127.0.0.1:8080 HTTPS_PROXY=http://127.0.0.1:8080 http_proxy=http://127.0.0.1:8080 https_proxy=http://127.0.0.1:8080
  export NO_PROXY= no_proxy=
  export NODE_EXTRA_CA_CERTS=/root/.mitmproxy/mitmproxy-ca-cert.pem SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
  sed "s#127.0.0.1:11434#$HIP:11434#" /opt/stillherd/opencode.json > /tmp/opencode.json
  export OPENCODE_CONFIG=/tmp/opencode.json OLLAMA_API_BASE=http://$HIP:11434
  export GOOSE_PROVIDER=ollama GOOSE_MODEL=qwen2.5-coder:7b OLLAMA_HOST=http://$HIP:11434
  first=1; i=0; done_turns=0
  while IFS= read -r line; do
    [ "$line" = "/exit" ] && break
    i=$((i+1)); O=/tmp/s_${tool}_${n}_${i}.out
    case "$tool" in
      opencode)
        A=""; [ $first -eq 0 ] && A="--continue"
        timeout 200 opencode run $A "$line" > "$O" 2>&1 < /dev/null ;;
      aider)
        A=""; [ $first -eq 0 ] && A="--restore-chat-history"
        timeout 200 /opt/aider/bin/aider --model ollama_chat/qwen2.5-coder:7b --message "$line" --yes-always --no-auto-commits --no-git $A calc.py test_calc.py > "$O" 2>&1 < /dev/null ;;
      goose)
        A=""; [ $first -eq 0 ] && A="--resume"
        timeout 200 goose run --name stillherd$n $A -t "$line" > "$O" 2>&1 < /dev/null ;;
    esac
    rc=$?; first=0
    b=$(wc -c < "$O")
    [ $rc -eq 0 ] && [ "$b" -gt 100 ] && done_turns=$((done_turns+1))
    echo "TURN $i rc=$rc out_bytes=$b" >&2
  done < /opt/stillherd/session.txt
  sleep 2; kill $MP 2>/dev/null; sleep 1
  echo "VERSION=$VER ROWS=$(wc -l < "$EGRESS_OUT") COMPLETED_TURNS=$done_turns"
  ;;
esac
