"""Runs inside the attested window, after the stack answers on :3170. Imports the sample Postman collection + environment into a
local Hoppscotch through the backend's REST/GraphQL API and checks that the 5 requests exist. Prints one RESULT line.
Local-only traffic: it talks to localhost and the hoppscotch containers, never to an external host."""

import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import convert

API = "http://localhost:3170"
JAR = http.cookiejar.CookieJar()
OP = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(JAR)
)
DB = "hoppscotch-hoppscotch-db-1"


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        r = OP.open(req, timeout=60)
        return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def gql(query, variables=None):
    s, j = call("POST", "/graphql", {"query": query, "variables": variables or {}})
    if s != 200 or (isinstance(j, dict) and j.get("errors")):
        raise SystemExit(f"graphql failed {s}: {json.dumps(j)[:400]}")
    return j["data"]


def wait_ping(n=120):
    for _ in range(n):
        try:
            if OP.open(API + "/ping", timeout=5).read().decode().strip() == "Success":
                return
        except Exception:
            pass
        time.sleep(3)
    raise SystemExit("backend did not answer /ping")


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True, check=True).stdout.strip()


wait_ping()
s, st = call("GET", "/v1/onboarding/status")
if not st["onboardingCompleted"]:
    s, r = call(
        "POST",
        "/v1/onboarding/config",
        {
            "VITE_ALLOWED_AUTH_PROVIDERS": "EMAIL",
            "MAILER_SMTP_ENABLE": "true",
            "MAILER_USE_CUSTOM_CONFIGS": "false",
            "MAILER_SMTP_URL": "smtp://host.docker.internal:2525",
            "MAILER_ADDRESS_FROM": "kit@example.test",
        },
    )
    if s not in (200, 201):
        raise SystemExit(f"onboarding failed {s} {r}")
    sh("docker", "restart", "hoppscotch-aio")
    time.sleep(5)
    wait_ping()
for _ in range(20):
    s, p = call("GET", "/v1/auth/providers")
    if s == 200 and p.get("providers") == ["EMAIL"]:
        break
    time.sleep(3)
s, r = call("POST", "/v1/auth/signin?origin=app", {"email": "kit@example.test"})
if s not in (200, 201):
    raise SystemExit(f"signin failed {s} {r}")
tok = sh(
    "docker",
    "exec",
    DB,
    "psql",
    "-U",
    "postgres",
    "-d",
    "hoppscotch",
    "-At",
    "-c",
    'select token from "VerificationToken" order by "expiresOn" desc limit 1',
)
s, r = call(
    "POST", "/v1/auth/verify", {"token": tok, "deviceIdentifier": r["deviceIdentifier"]}
)
if s not in (200, 201):
    raise SystemExit(f"verify failed {s} {r}")
col = convert.collection(os.path.join(HERE, "sample", "collection.postman.json"))
gql(
    "mutation($j:String!){ importUserCollectionsFromJSON(jsonString:$j, reqType: REST){ exportedCollection } }",
    {"j": json.dumps(col)},
)
ename, evars = convert.environment(
    os.path.join(HERE, "sample", "environment.postman.json")
)
gql(
    "mutation($n:String!,$v:String!){ createUserEnvironment(name:$n, variables:$v){ id name } }",
    {"n": ename, "v": json.dumps(evars)},
)
roots = gql("{ rootRESTUserCollections { id title } }")["rootRESTUserCollections"]
reqs = []
for c in roots:
    reqs += gql(
        "query($c:ID!){ userRESTRequests(collectionID:$c){ id title type } }",
        {"c": c["id"]},
    )["userRESTRequests"]
envs = (
    gql("{ me { environments { name variables } } }")["me"]["environments"]
    if False
    else None
)
titles = sorted(x["title"] for x in reqs)
want = sorted(
    i["name"]
    for i in json.load(open(os.path.join(HERE, "sample", "collection.postman.json")))[
        "item"
    ]
)
ok = titles == want
print(
    "RESULT "
    + json.dumps(
        {
            "collections": [c["title"] for c in roots],
            "requests_found": titles,
            "requests_expected": want,
            "match": ok,
        }
    )
)
sys.exit(0 if ok else 1)
