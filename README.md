# OParl Endpoint Scout (German Council Data Availability)

**Which German municipalities actually publish their council data, and can a program read it?**

One run checks a list of OParl entry points and reports, per endpoint, what is really there:
does the server answer, is the body actually an OParl `System` object, which vendor and OParl
version, which licence does the endpoint itself declare, how many bodies (Körperschaften) does
it list, and which collections — meetings, papers, organisations — can be read.

OParl is the German open-data standard for parliamentary and council information systems
(Ratsinformationssysteme). Around 11 000 municipalities exist in Germany; only a few hundred
publish an OParl interface, and a meaningful share of those are broken, half-finished or
protected against programs. This Actor is the survey step: before you build anything on top of
council data, you find out where the usable endpoints are.

## Why this exists

If you want to follow German municipal politics — motions, construction projects, tenders,
budget decisions — you need the endpoints. The community registry
([github.com/OParl/resources](https://github.com/OParl/resources)) lists candidate URLs, but a
URL in a list is not a working interface. Measured on 23 Nov 2026 across a sample of endpoints:

- one vendor answers **every** path with HTTP 200 plus an OParl `Error` object, so a naive
  scraper counts a dead server as a fully populated one;
- some vendors serve collections **only under a body**, and the plural path under the body id is
  a 404 — the working links are the singular ones advertised by the `Body` object itself;
- several vendors silently ignore `offset` or `limit`, so paging produces the same 100 items
  forever;
- some hosts rate-limit aggressively (HTTP 429 behind a WAF) when probed in parallel.

So this Actor does not trust the status code. Every payload is checked for its OParl `type`, a
collection counts only when a real collection object comes back, and a 429 is reported as
`rate_limited` rather than counted as a dead endpoint.

## Input

| Field | Default | Meaning |
| --- | --- | --- |
| `endpoints` | `[]` | Your own list of OParl entry URLs. Empty means: fetch the community registry at run time. |
| `filterText` | `""` | Keep only endpoints whose URL or municipality title contains this text, e.g. `sitzung-online` or `dresden`. |
| `maxEndpoints` | `25` | Cap per run. Endpoints are sorted by URL before the cap, so the covered set is predictable; the report always states how many were skipped. |
| `probeAllCollections` | `false` | Off: meetings, papers, organisations (3 requests per endpoint). On: all eleven collections. |
| `concurrency` | `6` | Endpoints checked in parallel. Keep it low: these are shared public servers. |
| `timeoutSeconds` | `15` | Per-request timeout. |

Example input:

```json
{
  "filterText": "sitzung-online",
  "maxEndpoints": 40,
  "concurrency": 4
}
```

## Output

One record per checked endpoint:

```json
{
  "title": "Stadt Leipzig",
  "entryUrl": "https://www.leipzig.sitzung-online.de/oparl/system",
  "checkedAt": "2026-11-23T23:45:00Z",
  "httpStatus": 200,
  "latencyMs": 410,
  "status": "alive",
  "oparlVersion": "1.1",
  "systemName": "Sitzungsdienst Leipzig",
  "vendor": "https://www.cc-egov.de",
  "license": "https://creativecommons.org/licenses/by/4.0/",
  "bodiesCount": 1,
  "collections": { "meetings": 10 },
  "collectionRoutes": { "meetings": "body_link" },
  "registry": "https://raw.githubusercontent.com/OParl/resources/master/endpoints.yml"
}
```

`status` is one of:

- `alive` — a real OParl System object, and at least one collection could be read.
- `answers_but_no_collections` — a System object, but no readable collection was found.
- `error_object` — HTTP 200 with an OParl `Error` body (a known vendor's behaviour).
- `not_oparl_system` — answered, but the body is not an OParl System object.
- `unreachable` — DNS, TLS, connection refused or timeout.
- `rate_limited` — the server pushed back with HTTP 429; lower `concurrency` and retry.

With `probeAllCollections` on, `collections` lists every collection that answered and
`collectionRoutes` says for each whether it was reachable at the top level or only through a
link from the `Body` object.

The last dataset item is the run report (`_report: true`): how many endpoints were seen,
scanned and skipped, the status tally, how many declare a licence, and which vendors appear.

## Cost and fairness

A default run (25 endpoints, core collections) is roughly 100 small HTTP GETs and well under a
tenth of a compute unit. Municipal servers are public infrastructure: the default concurrency is
deliberately modest, and the Actor stops probing an endpoint as soon as it is told to slow down.

## Data source and licence

The endpoint list is fetched at run time from
[OParl/resources](https://github.com/OParl/resources), the community registry maintained by the
OParl initiative; each record carries the `registry` field so the origin stays visible. The
licence of the *council data* behind an endpoint is whatever that endpoint declares in its
`license` field — this Actor reports it and does not interpret it. Many endpoints declare no
licence at all; that is a finding, not a permission.

## Limits

- OParl 1.0 and 1.1 entry points only.
- Read-only: this Actor never writes to a municipal system.
- A run covers at most 200 endpoints; the registry currently holds ~130 unique URLs, and the
  wider German landscape (~11 000 municipalities) is mostly without an interface at all.
