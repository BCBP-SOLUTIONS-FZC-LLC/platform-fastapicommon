# Enterprise Platform Observability Standard — fastapicommon implementation

fastapicommon implements the standard for Python FastAPI / gRPC services exactly as
[platform-gincommon](https://github.com/BCBP-SOLUTIONS-FZC-LLC/platform-gincommon) does for Go
services. **Go is the source of truth**: the registry and the reference rules are gincommon's,
vendored byte for byte, and CI fails when they drift (see [CI enforcement](#ci-enforcement)).

| Piece | Location |
|---|---|
| Platform Observability Registry (platform + domain metrics, label vocabulary, rules) | [`src/fastapicommon/metrics/registry.json`](../../src/fastapicommon/metrics/registry.json) — a copy of gincommon's `internal/obsregistry/registry.json` — rendered as [`metric-registry.md`](metric-registry.md) |
| Central label injection (`domain`, `service`, `environment`) | `PlatformSettings` → `observability_domain` (`OBSERVABILITY_DOMAIN`), `environment` (`APP_ENV` → `ENVIRONMENT`), `app_name`; `fastapicommon.metrics.init_metrics` |
| Shared metric constructors (registry-driven) | `fastapicommon.metrics.new_shared_counter` / `new_shared_gauge` / `new_shared_histogram` |
| Service-specific metric constructors (Tier 3, validated) | `fastapicommon.metrics.new_counter` / `new_gauge` / `new_histogram` |
| Identity labels for third-party collectors | `fastapicommon.metrics.register_labeled` |
| Compliance checks for services' CI | `fastapicommon.metrics.compliance.validate` / `validate_text`, the `fastapicommon-metricslint` CLI |
| Reference recording rules, SLO burn-rate alerts | [`docker/prometheus/rules/`](../../docker/prometheus/rules/) (gincommon's, vendored) |
| Removed pre-standard names and their replacements | [`migration.md`](migration.md) |
| Alert runbooks | [`runbooks.md`](runbooks.md) |

The Python API mirrors gincommon's Go API one to one:

| platform-gincommon (Go) | fastapicommon (Python) |
|---|---|
| `obsregistry.Parse` / `Default` | `fastapicommon.metrics.registry.parse_registry` / `default_registry` |
| `gincommon.NewCounterVec`, `NewCounter`, `NewGaugeVec`, `NewGauge`, `NewHistogramVec` | `new_counter`, `new_gauge`, `new_histogram` (labels optional) |
| `gincommon.NewSharedCounterVec`, `NewSharedGaugeVec`, `NewSharedHistogramVec` | `new_shared_counter`, `new_shared_gauge`, `new_shared_histogram` |
| `gincommon.LabeledRegisterer` | `register_labeled` |
| `gincommon.MetricsGatherer` | `metrics_registry` |
| `pkg/obsregistry.Validate`, `ValidateText`, `Options`, `Report` | `compliance.validate`, `validate_text`, `Options`, `Report` |
| `metricslint check` / `refs` / `registry` / `inventory` | `fastapicommon-metricslint check` / `refs` / `registry` / `inventory` (same flags, `--` prefixed, same exit codes) |

## What fastapicommon emits

The same `platform_http_*` and `platform_grpc_*` metrics as gincommon (unary gRPC only), plus
`platform_build_info{version}`, with identical names, help text, labels, label values and
buckets — so dashboards, alerts and SLOs are portable between Go and Python services. The
[README](../../README.md#platform-observability-standard-metrics) lists them with the code path
that records each one. `tests/integration/test_metrics_compliance.py` drives every code path
and checks the scrape with `validate()`: 0 errors.

`python_*` and `process_*` runtime collectors are exempt like `go_*`. prometheus_client's
`<metric>_created` companion families are classified with their parent (a gauge or untyped
`X_created` whose series carry exactly the label sets of the counter `X_total` or the
histogram/summary `X`); fastapicommon's own collectors and those built with the constructors
below emit none, as Go's client does.

## Tiers

| Tier | Namespace | Required labels | Who may add a metric |
|---|---|---|---|
| 1 — platform-shared | `platform_*` | `domain`, `service`, `environment` | Observability governance only (registry PR in platform-gincommon) |
| 2 — domain-shared | `<domain>_*` | `service`, `environment` | Observability governance, with the domain's owners (registry PR in platform-gincommon) |
| 3 — service-specific | `<domain>_<service>_*` | `service`, `environment` | The service team, no registry entry needed |

Decision tree: can services in **several domains** emit it with identical meaning →
`platform_*`. Otherwise, can **several services in one domain** → `<domain>_*`. Otherwise →
`<domain>_<service>_<metric>`.

The Tier 3 prefix is derived from the domain and `app_name`: `iam` + `event-consumer` (or
`iam-event-consumer`) → `iam_event_consumer_` (`fastapicommon.metrics.registry.service_prefix`).

`domain` is carried on Tier 2/3 too, because the same const labels are injected on every
collector. `version` is **not** a const label: it is only on `platform_build_info`, because a
per-build label on every series would multiply series on each deploy.

## Enabling the standard in a service

```python
settings = PlatformSettings(
    app_name="event-consumer",     # service label (^[a-z][a-z0-9_-]{0,62}$)
    observability_domain="iam",    # or OBSERVABILITY_DOMAIN
    environment="prod",            # or APP_ENV, then ENVIRONMENT
    build_version=version,         # or BUILD_VERSION
)
app = create_platform_app(settings=settings)
```

- **The domain is required.** `create_platform_app()` (and `ContextServerInterceptor(settings)`)
  raise `ValueError` at startup — where gincommon panics — on a missing domain, a domain that is
  not in the registry, an `app_name` that is not a valid service label, or an environment outside
  `local, dev, test, staging, prod` (after trimming and lowercasing). There are no aliases:
  `production` is rejected. platform-eventcommon and platform-pgcommon-py read the same
  variables in the same order, so every library in the process reports the same identity.
- **One identity per process.** The first initialization fixes `domain`, `service`,
  `environment` and `version`, like gincommon's `sync.Once`. In tests, call
  `init_metrics(options, registry=CollectorRegistry())` to get an isolated registry.
- The metric constructors raise `MetricsNotInitializedError` until metrics are initialized,
  because the identity labels are not known yet.

### Shared metrics (Tier 1 and 2)

```python
from fastapicommon.metrics import new_shared_counter, new_shared_histogram

processed = new_shared_counter("platform_messages_processed_total")
# labels come from the registry, in registry order: queue, event_type
processed.labels(queue="iam.events", event_type="user.created").inc()

auth = new_shared_counter("iam_auth_events_total")  # only in domain iam
dependency = new_shared_histogram("platform_dependency_request_seconds")  # registry buckets
```

Name, help text, label names and buckets all come from the registry entry, so instrumentation
code cannot drift from the contract. A domain metric can only be created by a service in that
domain. Label **values** must stay inside the vocabulary (for example `outcome` ∈ `success,
failure, error`); `validate()` checks this.

### Service-specific metrics (Tier 3)

```python
from fastapicommon.metrics import new_counter, new_histogram

hmac_failures = new_counter(
    "iam_event_consumer_hmac_failures_total", "HMAC verification failures", ["reason"]
)
lookups = new_histogram(
    "iam_event_consumer_tenant_lookup_seconds", "Tenant lookup latency", ["cache"]
)  # buckets=None: the registry's request_duration preset
```

The returned objects are plain `prometheus_client` metrics registered on the registry `/metrics`
serves; the identity labels are added when they are collected, so `.labels(...)` takes only the
metric's own labels. The constructors raise `ValueError` for:

- registry names and anything under `platform_*`
- counters without `_total`, histograms without `_seconds`, and gauges ending in `_total`
- prohibited labels: `user_id`, `email`, `tenant_id`, `request_id`, `event_id`, `session_id`,
  `trace_id`, ..., and any label ending in `_id`, `_email`, `_ip`, `_uuid` or `_token`
- declaring a centrally injected label
- any name outside the service's `<domain>_<service>_` prefix

### Libraries that register their own collectors

platform-eventcommon and platform-pgcommon-py inject the identity labels themselves; give them
the same domain, service and environment, and register them on `metrics_registry()` so one
`/metrics` serves everything. For other third-party collectors,
`register_labeled(collector)` adds `domain`, `service` and `environment` to every sample. Do not
wrap the platform libraries' collectors with it: the labels would be duplicated.

## CI enforcement

**This repository** (`make metrics-lint` and `make gincommon-parity`, in `validate-quality.yml`):

- `fastapicommon-metricslint registry`: the vendored registry is self-consistent (the same
  rules as gincommon's `metricslint registry`: namespaces match tiers, suffixes, labels in the
  vocabulary and not prohibited, shared metrics have help and aggregation expectations, legacy
  entries have a replacement and a sunset).
- `fastapicommon-metricslint refs --deny-deprecated docker/prometheus/rules`: the reference rules
  use only registered `platform_*` names.
- `promtool check rules` + `promtool test rules`: the rules are valid and the burn-rate alerts
  fire as intended.
- Inventory drift: `metric-registry.md` matches the registry (`make metrics-inventory`
  regenerates it).
- **gincommon parity**: `registry.json` and `docker/prometheus/rules/*` are byte-identical to
  platform-gincommon's at `GINCOMMON_REF`, and `metric-registry.md` equals gincommon's (except for
  the source path in its header). Required on `main` and for a release; a PR without access to
  the private repo (a fork) only warns.
- `tests/integration/test_metrics_compliance.py`: fastapicommon's own emitted metrics pass
  `validate()` with 0 errors.

**Consuming services** should add both of these:

```python
# tests/test_metrics_compliance.py: build the real app, drive one request, then:
from fastapicommon.metrics import metrics_registry
from fastapicommon.metrics.compliance import validate


def test_metrics_compliance(client):
    client.get("/widgets/me")
    report = validate(metrics_registry())
    assert report.ok(), "\n".join(map(str, report.errors()))
```

> **Strict mode currently fails on every fastapicommon service.** Every metric fastapicommon
> emits is `status: proposed` until observability governance ratifies it (see gincommon's
> *Items awaiting ratification*). `--strict` / `Options(strict=True)` turns proposed entries
> into errors, so it fails on fastapicommon's own metrics regardless of what the service does.
> Until ratification, either run without strict mode (proposed entries are warnings), or exempt
> the platform libraries' metrics: `Options(strict=True, allow_proposed_owners=("platform-gincommon",
> "platform-events", "platform-pgcommon"))` (fastapicommon's entries are owned by
> `platform-gincommon`, the registry owner of the HTTP / gRPC metrics). The Tier 2 `iam_*`
> metrics have no owner and stay errors under strict mode.

```bash
# Dashboards, alerts, recording rules, SLOs, HPA manifests, runbooks:
fastapicommon-metricslint refs deploy/ dashboards/
# Live scrape, e.g. in an e2e job:
fastapicommon-metricslint check --url http://localhost:8080/metrics
```

Exit codes: **0** compliant, **1** findings (for `registry`: the document is readable but
violates the standard), **2** usage or I/O error. `check` accepts the Prometheus text format and
OpenMetrics text (recognised by its `# EOF` line). `refs` walks directories recursively, skipping
`.git`, `node_modules` and `vendor`; an unreadable path or a file over 64 MiB is reported as an
error finding, the scan continues, and the exit code is **2**.

What `validate()` / `fastapicommon-metricslint check` enforce on every emitted metric:

| Rule | Check |
|---|---|
| `namespace` | Classified as platform / domain / service. Unregistered `platform_*` and `<domain>_*` names fail. A Tier 3 name must match its own service and domain. |
| `name-format`, `suffix` | snake_case; `_total` for counters, `_seconds` for histograms, no `_total` on gauges |
| `type` | Matches the registry type; summaries and untyped metrics are not allowed |
| `required-label` | `domain`, `service` and `environment` present as the tier requires |
| `label-vocabulary` | Shared metrics carry exactly the registry labels plus const labels |
| `prohibited-label` | No high-cardinality labels on any tier |
| `label-name` | Service-owned labels on Tier 3 metrics are snake_case `[a-z0-9_]` |
| `label-value` | Values inside the enum/pattern of every vocabulary label |
| `cardinality` | Distinct values per label within the registry's `max_values` |
| `status` | `proposed` entries warn (error with strict mode, except allowed owners) |
| `gather` | (`validate()` only) a collector failed while collecting |
| `removed` | A pre-standard name that was removed: emitting or querying it is an **error** naming the replacement |
| `deprecated` | (Future migrations only) a legacy name still emitted during a compatibility period: a warning |

## Changing the registry (governance)

The registry is owned by platform-gincommon: a change is a PR to gincommon's
`internal/obsregistry/registry.json` (see its `docs/observability/README.md` for what the entry
must include and how ratification works). Once released, re-copy it here:

1. Copy gincommon's `internal/obsregistry/registry.json` to
   `src/fastapicommon/metrics/registry.json` (and `docker/prometheus/rules/*` if they changed).
2. Bump `GINCOMMON_REF` in `.github/workflows/validate-quality.yml` to that release.
3. `make metrics-inventory` to regenerate `metric-registry.md`, then `make metrics-lint`.
4. If a new `platform_http_*` / `platform_grpc_*` metric was added, emit it here too and add it
   to `tests/integration/test_metrics_compliance.py`.

## SLOs

The vendored reference rules implement the following SLIs and SLOs. Copy them into a service's
rule set and adjust the targets per service.

| SLO | SLI (recording rule) | Target | Alerts |
|---|---|---|---|
| HTTP availability | `service:platform_http_error_ratio:rate{5m,30m,1h,6h}`: share of `status_class="5xx"` in `platform_http_requests_total` | 99.9% / 30d | `PlatformHTTPErrorBudgetFastBurn` (14.4×, 1h+5m, page), `PlatformHTTPErrorBudgetSlowBurn` (6×, 6h+30m, ticket) |
| gRPC availability | `service:platform_grpc_error_ratio:rate*` over `platform_grpc_server_handled_total` | 99.9% / 30d | `PlatformGRPCErrorBudget{Fast,Slow}Burn` |
| HTTP latency | `service_route:platform_http_request_duration_seconds:p99_5m` | p99 < 1s | `PlatformHTTPLatencyHigh` |
| Message processing | `service_queue:platform_messages_failed_ratio:rate5m` | < 5% failures | `PlatformMessagesFailureRatioHigh`, `PlatformMessagesDeadLettered` |

All SLIs keep `domain`, `service` and `environment`, so `sum by (domain, environment)` gives a
domain-level view and `sum by (environment)` a platform-level one. `make docker-up` loads the
rules into the local Prometheus.

## HPA

Scale on the platform names through prometheus-adapter (see [migration](migration.md#hpa)).
The recording rules `service:platform_http_active_requests:sum` and
`service:platform_grpc_server_inflight_requests:sum` are meant as HPA inputs.
