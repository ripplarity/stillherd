cd selfhost/recipes/hoppscotch || exit 1
docker compose -f docker-compose.yml -f docker-compose.override.yml --env-file .env --profile default up -d || exit 1
ok=0
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://localhost:3170/ping")
  case "$code" in 2*) ok=1; break;; esac
  sleep 10
done
[ $ok = 1 ] || { echo "SMOKE failed last=$code"; exit 1; }
echo "SMOKE ok $code after $((i*10))s"
cd ../../.. && true
