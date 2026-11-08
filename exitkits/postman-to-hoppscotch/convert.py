"""Postman v2.1 collection/environment -> Hoppscotch collection/environment JSON (stdlib only).
Hoppscotch's own Postman importer ships only inside its browser bundle, so this mapping is applied here and the result is sent to the
backend's importUserCollectionsFromJSON / createUserEnvironment mutations. Mapped: name, method, URL, headers, raw body, collection bearer auth, {{var}}."""
import json, re, sys

V = lambda s: re.sub(r"\{\{([^}]+)\}\}", r"<<\1>>", s or "")


def request(item):
    r = item["request"]
    ct = None
    hdrs = [{"key": h["key"], "value": V(h.get("value", "")), "active": not h.get("disabled", False), "description": ""} for h in r.get("header", [])]
    for h in hdrs:
        if h["key"].lower() == "content-type":
            ct = h["value"].split(";")[0]
    body = {"contentType": None, "body": None}
    if r.get("body", {}).get("mode") == "raw":
        body = {"contentType": ct or "application/json", "body": V(r["body"]["raw"])}
    return {"v": "16", "name": item["name"], "method": r["method"], "endpoint": V(r["url"]["raw"]), "params": [], "headers": hdrs,
            "preRequestScript": "", "testScript": "", "auth": {"authType": "inherit", "authActive": True}, "body": body,
            "requestVariables": [], "responses": {}}


def folder(items, name, auth=None):
    reqs = [request(i) for i in items if "request" in i]
    subs = [folder(i["item"], i["name"]) for i in items if "item" in i]
    a = {"authType": "none", "authActive": True}
    if auth and auth.get("type") == "bearer":
        tok = next(x["value"] for x in auth["bearer"] if x["key"] == "token")
        a = {"authType": "bearer", "authActive": True, "token": V(tok)}
    return {"v": 11, "name": name, "folders": subs, "requests": reqs, "auth": a, "headers": [], "variables": []}


def collection(p):
    c = json.load(open(p))
    return [folder(c["item"], c["info"]["name"], c.get("auth"))]


def environment(p):
    e = json.load(open(p))
    return e["name"], [{"key": v["key"], "value": v["value"], "secret": False} for v in e["values"] if v.get("enabled", True)]


if __name__ == "__main__":
    kind, path = sys.argv[1], sys.argv[2]
    if kind == "collection":
        print(json.dumps(collection(path)))
    else:
        n, v = environment(path)
        print(json.dumps({"name": n, "variables": json.dumps(v)}))
