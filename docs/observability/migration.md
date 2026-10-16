# Removed pre-standard metric names

fastapicommon's pre-standard metric names (`http_requests_total`, `grpc_requests_total`, ...)
broke the Enterprise Platform Observability Standard in the same ways as gincommon's:

- They had no namespace.
- They lacked the `domain` and `environment` labels.
- One label name had two meanings across HTTP and gRPC.

The standard's backward-compatibility process (emit both, then migrate, deprecate and remove)
exists to protect dashboards and alerts that already query the old names. No platform library
(gincommon, fastapicommon, platform-events, platform-pgcommon or their Python ports) had been
deployed to production or dev, so there were no such series to protect. The old names were
therefore **removed outright, with no compatibility period**. fastapicommon emits only
`platform_*`.

## Name mapping

| Removed name | Replacement | Label changes |
|---|---|---|
| `http_requests_total{method, path, status_code}` | `platform_http_requests_total` | + `domain`, `environment`; `path` → `route` (the route template in Gin syntax, `/items/:item_id`, never the raw path); `status_code` → `status_class` + `error_class`; non-standard `method` values become `OTHER` |
| `http_request_duration_seconds{method, path}` | `platform_http_request_duration_seconds` | + `domain`, `environment`, `status_class`, `error_class`; `path` → `route`; the registry's `request_duration` buckets |
| `grpc_requests_total{method, status_code}` | `platform_grpc_server_handled_total` | + `domain`, `environment`; `method` → `grpc_method`; `status_code` → `grpc_code` (Go-style code names, e.g. `NotFound`) + `status_class` + `error_class` |
| `grpc_request_duration_seconds{method}` | `platform_grpc_server_handling_seconds` | + `domain`, `environment`, `status_class`, `error_class`; `method` → `grpc_method` |

fastapicommon also emits the platform series that had no pre-standard counterpart here
(`platform_build_info`, `platform_http_request_size_bytes`, `platform_http_response_size_bytes`,
`platform_http_active_requests`, `platform_http_panics_total`,
`platform_http_rate_limited_requests_total`, `platform_http_request_timeouts_total` and the
`platform_grpc_*` equivalents), exactly as gincommon does.

`version` is only on `platform_build_info`; to break a query down by build, join on it. All
removed names — fastapicommon's and the other libraries' — stay in the central
[registry](metric-registry.md#legacy-metrics-removed-or-deprecated) with status `removed`, so
`fastapicommon-metricslint refs` turns any remaining use into an error naming the replacement.

## Upgrading a consuming service

There is no parallel-emission phase, so do this in the same change that bumps fastapicommon:

1. **Set the identity.** Set `PlatformSettings.observability_domain` (or `OBSERVABILITY_DOMAIN`,
   e.g. in the Helm values), and make sure the environment resolves (`environment=`, else
   `APP_ENV`, else `ENVIRONMENT`) to one of `local, dev, test, staging, prod`. `production` is
   rejected. The service will not start without a valid domain.
2. **Find every reference.** Run `fastapicommon-metricslint refs` over everything that queries
   metrics: dashboards, alert rules, recording rules, SLO definitions, HPA / prometheus-adapter
   config, release health gates, runbooks. Every hit is an error naming the replacement:
   ```bash
   uv run fastapicommon-metricslint refs deploy/ .github/ dashboards/
   # deploy/hpa.yaml:14: error: legacy metric grpc_requests_total was removed and is no longer emitted; use platform_grpc_server_handled_total
   ```
3. **Rewrite the queries** with the table above. Group gRPC queries by `grpc_method`. Remove
   `version` from selectors and groupings except on `platform_build_info`.
4. **Lock it in.** Add `fastapicommon-metricslint refs <paths>` (it fails on removed names) and a
   test with `fastapicommon.metrics.compliance.validate` to the service's CI.

## PromQL examples

```promql
# before
sum by (path) (rate(http_requests_total{status_code=~"5.."}[5m]))
# after (aggregates across services and domains)
sum by (domain, service, environment, route) (rate(platform_http_requests_total{status_class="5xx"}[5m]))

# before
histogram_quantile(0.99, sum by (le, method) (rate(grpc_request_duration_seconds_bucket[5m])))
# after
histogram_quantile(0.99, sum by (le, grpc_method) (rate(platform_grpc_server_handling_seconds_bucket[5m])))
```

## HPA

prometheus-adapter rule after the migration:

```yaml
- seriesQuery: 'platform_http_active_requests{namespace!="",pod!=""}'
  name: { as: "platform_http_active_requests" }
  metricsQuery: 'sum(<<.Series>>{<<.LabelMatchers>>}) by (<<.GroupBy>>)'
```

Update the `HorizontalPodAutoscaler`'s `metric.name` to the new name in the same change, since
the adapter exposes the renamed metric.
