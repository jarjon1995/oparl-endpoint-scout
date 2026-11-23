#!/usr/bin/env python3
"""OParl endpoint scout: find and health-check German council-data endpoints.

One run answers, for a list of OParl entry points, what is actually there:
does the server answer, is the body really an OParl System object, which
vendor and OParl version, which licence does it declare, how many bodies,
and which collections (meetings, papers, organisations) can be read.

Stdlib only, so the image stays small and there is nothing to break in a build.

The design rule comes from measurement on 12 endpoints (23 Nov 2026), not from
the specification: HTTP 200 is not evidence. One vendor answers every path with
200 plus an OParl *Error* object; another serves collections only under a body
and silently ignores `offset`. So every payload is checked for its OParl
`type`, and a collection counts only when a real collection object comes back.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "oparl-scout/0.1 (Apify Actor; read-only; contact: anselm@posteo.de)"
REGISTRY_URL = ("https://raw.githubusercontent.com/OParl/resources/master/"
                "endpoints.yml")
COLLECTIONS_FULL = ("organizations", "people", "locations", "meetings",
                    "agendas", "papers", "drafts", "files", "committees",
                    "participations", "elections")
COLLECTIONS_CORE = ("meetings", "papers", "organizations")


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

def get(url, timeout):
    """(status, parsed_json_or_None, note). Never raises."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(400_000), r.status
    except urllib.error.HTTPError as e:
        raw = e.read(4000) if e.fp else b""
        status = e.code
    except Exception as exc:                       # timeout, DNS, TLS, refused
        return None, None, "%s: %s" % (type(exc).__name__, exc)
    try:
        return status, json.loads(raw), None
    except Exception:
        return status, None, "non-JSON body: %s" % raw[:120].decode(
            "utf-8", "replace")


def oparl_type(payload):
    return str(payload.get("type", "")) if isinstance(payload, dict) else ""


def is_system(payload):
    """A 200 with parseable JSON is NOT a System object (measured, zv-kisa)."""
    if not isinstance(payload, dict):
        return False
    return oparl_type(payload).endswith("/System") or "oparlVersion" in payload


def is_error(payload):
    return oparl_type(payload).endswith("/Error")


def collection(payload):
    """Normalise both collection shapes; None for anything that is not one."""
    if not isinstance(payload, dict) or is_error(payload):
        return None
    items = payload.get("data")
    if not isinstance(items, list):
        items = payload.get("items")
    if not isinstance(items, list):
        return None
    paging = {k: payload[k] for k in
              ("pagination", "page", "total", "totalElements",
               "numberOfElements") if k in payload}
    return {"count": len(items), "paging": paging}


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

def parse_registry(text):
    """Minimal parser for the OParl/resources endpoints.yml.

    Only the `title` and `url` keys of each list item are read; the file also
    carries osm/wd ids and free-text descriptions that are not needed here.
    """
    out, cur, seen = [], {}, set()

    def flush():
        url = cur.get("url")
        if url and url not in seen:
            seen.add(url)
            out.append({"title": cur.get("title", ""), "url": url})

    for line in text.splitlines():
        if line.startswith("- "):
            flush()
            cur = {}
            if ":" in line[2:]:
                k, v = line[2:].split(":", 1)
                cur[k.strip()] = v.strip()
        elif line.strip().startswith("url:") or line.strip().startswith("title:"):
            k, v = line.strip().split(":", 1)
            cur[k.strip()] = v.strip()
    flush()
    return out


def read_registry(timeout, log):
    """Fetch the community registry at run time instead of shipping a copy:
    the list changes and the upstream project owns it. Attribution is kept in
    every record's `registry` field."""
    req = urllib.request.Request(REGISTRY_URL, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read(600_000).decode("utf-8", "replace")
    except Exception as exc:
        log.append("registry fetch failed %s: %s" % (type(exc).__name__, exc))
        return []
    entries = parse_registry(text)
    log.append("registry %s -> %d unique endpoints" % (REGISTRY_URL, len(entries)))
    return entries


# --------------------------------------------------------------------------
# one endpoint
# --------------------------------------------------------------------------

def _trim(payload, n=25):
    """A collection fetched without a page size can be huge; keep the record
    small and honest about what was cut."""
    if not isinstance(payload, dict):
        return payload
    for key in ("data", "items"):
        if isinstance(payload.get(key), list) and len(payload[key]) > n:
            payload = dict(payload)
            payload[key] = payload[key][:n]
            payload["_truncatedAt"] = n
    return payload


def scout(entry, timeout, deep, log):
    rec = {"title": entry.get("title", ""), "entryUrl": entry["url"],
           "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    t0 = time.time()
    st, sysobj, note = get(entry["url"], timeout)
    rec["httpStatus"] = st
    rec["latencyMs"] = int((time.time() - t0) * 1000)
    if note:
        rec["status"] = "unreachable"
        rec["error"] = note
        return rec
    if not is_system(sysobj):
        rec["status"] = "error_object" if is_error(sysobj) else "not_oparl_system"
        rec["error"] = "body type=%r" % oparl_type(sysobj)
        return rec

    rec["status"] = "alive"
    rec["oparlVersion"] = (sysobj.get("oparlVersion") or "").replace(
        "https://schema.oparl.org/", "").strip("/")
    rec["systemName"] = sysobj.get("name")
    rec["vendor"] = sysobj.get("vendor")
    rec["product"] = sysobj.get("product")
    rec["license"] = sysobj.get("license")
    rec["contact"] = sysobj.get("contactName")

    bodies, body_obj, body_links = [], None, {}
    body_url = sysobj.get("body") or sysobj.get("bodies")
    if body_url:
        stb, bp, _ = get(body_url, timeout)
        rec["bodiesHttp"] = stb
        col = collection(bp)
        if col:
            src = bp.get("data") if isinstance(bp.get("data"), list) else bp.get("items")
            src = src or []
            bodies = [b.get("id") for b in src
                      if isinstance(b, dict) and b.get("id")]
            rec["bodiesCount"] = len(bodies)
            # Some vendors (measured 23 Nov on München Transparent) put the
            # collection links straight into the Body objects of the bodies
            # collection, so no extra request is needed to find them.
            for b in src:
                if isinstance(b, dict) and any(
                        isinstance(b.get(k), str) for k in
                        ("meeting", "paper", "organization", "person")):
                    body_links = b
                    break
        if bodies:
            # The OParl-correct way to find a body's collections: the Body
            # object itself names them. Measured 23 Nov on more! Rubin and on
            # STERNBERG RIM: the plural path under the body id is a 404, while
            # the Body object advertises singular links (meeting, paper,
            # organization, person) that answer. Guessing the plural path is
            # therefore not enough, and calling such an endpoint dead is wrong.
            stbo, bo, _ = get(bodies[0], timeout)
            if stbo == 200 and isinstance(bo, dict):
                body_obj = bo

    base = (sysobj.get("system") or entry["url"]).rstrip("/")
    wanted = COLLECTIONS_FULL if deep else COLLECTIONS_CORE
    avail, scoped, via = {}, None, {}
    for c in wanted:
        stc, cp, _ = get("%s/%s?limit=1" % (base, c), timeout)
        if stc == 429:
            rec["status"] = "rate_limited"
            rec["error"] = "HTTP 429 while probing collections"
            return rec
        col = collection(cp) if stc == 200 else None
        if col:
            avail[c] = col["count"]
            via[c] = "top_level"
    if not avail:
        # The Body object names its own collections; guessing the plural path
        # under the body id is not enough (measured: it is a 404 on more! Rubin
        # and STERNBERG RIM). Singular keys first, then the plural guess.
        source = body_links or (body_obj or {})
        for c in wanted:
            link = (source.get(c) or source.get(c.rstrip("s"))
                    or source.get(c.lower()))
            if not isinstance(link, str) and bodies:
                link = "%s/%s" % (bodies[0], c)
            if not isinstance(link, str):
                continue
            stc, cp, _ = get("%s?limit=1" % link, timeout)
            if stc == 400:
                # Measured on STERNBERG RIM: the collection exists but refuses
                # a page size with HTTP 400. Retry without the parameter.
                stc, cp, _ = get(link, timeout)
                cp = _trim(cp)
            if stc == 429:
                rec["status"] = "rate_limited"
                rec["error"] = "HTTP 429 while probing collections"
                return rec
            col = collection(cp) if stc == 200 else None
            if col:
                avail[c] = col["count"]
                scoped = bodies[0]
                via[c] = "body_link"
                break
    rec["collections"] = avail
    rec["collectionsScopedUnderBody"] = scoped
    rec["collectionRoutes"] = via
    if not avail:
        rec["status"] = "answers_but_no_collections"
    return rec


# --------------------------------------------------------------------------
# platform plumbing (input / dataset), same proven routes as oparl-reader
# --------------------------------------------------------------------------

_TOKEN_ENV = ("APIFY_TOKEN", "APIFY_API_TOKEN", "APFY_API_TOKEN",
              "ACTOR_API_TOKEN")
_STORE_ENV = ("APIFY_DEFAULT_KEY_VALUE_STORE_ID",
              "ACTOR_DEFAULT_KEY_VALUE_STORE_ID")
_DATASET_ENV = ("APIFY_DEFAULT_DATASET_ID", "ACTOR_DEFAULT_DATASET_ID")


def _env(names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v, n
    return None, None


def read_input_via_sdk():
    try:
        from apify_client import ApifyClient
        store = os.environ.get("APIFY_DEFAULT_KEY_VALUE_STORE_ID")
        if not store:
            return None, "sdk: no store id in env"
        rec = ApifyClient().key_value_store(store).get_record("INPUT")
        if isinstance(rec, dict) and rec:
            return rec, "sdk rest"
        return None, "sdk: INPUT record empty or missing"
    except Exception as exc:
        return None, "sdk input FAILED %s: %s" % (type(exc).__name__, exc)


def read_input():
    diag = {"env_present": sorted(k for k in os.environ
                                  if k.startswith(("APIFY_", "APFY_", "ACTOR_")))}
    rec, why = read_input_via_sdk()
    diag["sdk"] = why
    if rec:
        diag["input_source"] = "sdk"
        return rec, diag
    token, tname = _env(_TOKEN_ENV)
    store, sname = _env(_STORE_ENV)
    diag["token_env"] = tname
    diag["store_env"] = sname
    if token and store:
        url = ("https://api.apify.com/v2/key-value-stores/%s/records/INPUT"
               "?token=%s" % (store, token))
        st, payload, note = get(url, 30)
        diag["input_http"] = st
        if note:
            diag["input_note"] = note
        if isinstance(payload, dict) and payload:
            diag["input_source"] = "rest"
            return payload, diag
    if os.path.exists("INPUT.json"):
        with open("INPUT.json", encoding="utf-8") as fh:
            diag["input_source"] = "INPUT.json"
            return json.load(fh), diag
    diag["input_source"] = "none"
    return {}, diag


def push_via_sdk(items):
    try:
        from apify_client import ApifyClient
    except Exception as exc:
        return "sdk import FAILED %s: %s" % (type(exc).__name__, exc)
    try:
        client = ApifyClient()
        ds = os.environ.get("APIFY_DEFAULT_DATASET_ID")
        if ds:
            client.dataset(ds).push_items(items)
            return "sdk dataset %s n=%d" % (ds, len(items))
        rid = os.environ.get("APIFY_ACTOR_RUN_ID")
        if rid:
            did = client.run(rid).get().get("defaultDatasetId")
            client.dataset(did).push_items(items)
            return "sdk dataset via run %s n=%d" % (did, len(items))
        return "sdk: no dataset id in env"
    except Exception as exc:
        return "sdk push FAILED %s: %s" % (type(exc).__name__, exc)


def push(items):
    if not items:
        return "none (no items)"
    r = push_via_sdk(items)
    if r.startswith("sdk dataset"):
        return r
    token, _ = _env(_TOKEN_ENV)
    ds, _ = _env(_DATASET_ENV)
    rest = "skipped (no token/dataset env)"
    if token and ds:
        url = "https://api.apify.com/v2/datasets/%s/items?token=%s" % (ds, token)
        body = json.dumps(items).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return "rest dataset %s http=%s n=%d" % (ds, resp.status,
                                                         len(items))
        except Exception as exc:
            rest = "rest FAILED %s: %s" % (type(exc).__name__, exc)
    with open("output.jsonl", "a", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    return "file only | %s | %s" % (r, rest)


# --------------------------------------------------------------------------

def run(inp):
    log = []
    timeout = min(max(int(inp.get("timeoutSeconds") or 15), 3), 60)
    deep = bool(inp.get("probeAllCollections"))
    max_endpoints = min(max(int(inp.get("maxEndpoints") or 25), 1), 200)
    concurrency = min(max(int(inp.get("concurrency") or 6), 1), 12)

    urls = inp.get("endpoints") or []
    if urls:
        entries = [{"title": "", "url": u} for u in urls]
        log.append("input list: %d endpoints" % len(entries))
    else:
        entries = read_registry(timeout, log)

    # deterministic order, then the cap: which endpoints a run covers must be
    # predictable for the user, not dependent on fetch order.
    entries.sort(key=lambda e: e["url"])
    if inp.get("filterText"):
        needle = inp["filterText"].lower()
        entries = [e for e in entries
                   if needle in e["url"].lower() or needle in e["title"].lower()]
        log.append("filter %r -> %d endpoints" % (inp["filterText"], len(entries)))
    scanned = entries[:max_endpoints]
    skipped = len(entries) - len(scanned)

    def work(e):
        try:
            return scout(e, timeout, deep, log)
        except Exception as exc:
            return {"entryUrl": e["url"], "status": "scout_error",
                    "error": "%s: %s" % (type(exc).__name__, exc)}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        recs = list(ex.map(work, scanned))
    for r in recs:
        r["registry"] = REGISTRY_URL if not urls else "user input"

    tally = {}
    for r in recs:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    report = {
        "endpointsSeen": len(entries),
        "endpointsScanned": len(scanned),
        "endpointsSkippedByMax": skipped,
        "statusTally": tally,
        "aliveWithCollections": sum(
            1 for r in recs if r["status"] == "alive"),
        "withLicense": sum(1 for r in recs if r.get("license")),
        "vendors": sorted({r["vendor"] for r in recs if r.get("vendor")}),
        "elapsedSeconds": round(time.time() - t0, 1),
        "deepProbe": deep,
        "log": log[-15:],
    }
    return recs, report


def main():
    inp, diag = read_input()
    try:
        recs, report = run(inp)
    except Exception:
        import traceback
        recs, report = [], {"fatal": "unhandled exception in run()",
                            "traceback": traceback.format_exc()[-3000:]}
    report["plumbing"] = diag
    report["items_total"] = len(recs)
    receipt = push(recs) if recs else "no items"
    report["delivery_items"] = receipt
    receipt_report = push([{"_report": True, "report": report}])
    report["delivery_report"] = receipt_report
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        print(traceback.format_exc())
        sys.exit(0)
