docker compose -f docker-compose.yml -f docker-compose.override.yml --env-file .env  up -d || exit 1
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://localhost/gotrue/health")
  case "$code" in 2*) echo "SMOKE ok $code after $((i*10))s"; exit 0;; esac
  sleep 10
done
echo "SMOKE failed last=$code"; exit 1
