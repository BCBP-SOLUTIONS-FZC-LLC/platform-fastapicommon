# Runbooks: platform alerts

Runbooks for the reference alerts in [`docker/prometheus/rules/platform-alerts.yml`](../../docker/prometheus/rules/platform-alerts.yml) (vendored from platform-gincommon, so the same alerts fire for Go and Python services). Every alert carries `domain`, `service` and `environment`. Start from those to find the owning team and the deployment. This copy describes what fastapicommon services log and emit.

## PlatformHTTPErrorBudgetFastBurn

**Meaning.** More than 1.44% of HTTP requests (14.4× the 0.1% budget) returned 5xx over both the last hour and the last 5 minutes. At this rate, 2% of the 30-day error budget burns per hour.

**Triage.**
1. Find where the errors come from: `sum by (route, error_class) (rate(platform_http_requests_total{service="$service",environment="$environment",status_class="5xx"}[5m]))`.
2. Check for a recent rollout: `count by (version) (platform_build_info{service="$service",environment="$environment"})`.
3. Look for unhandled exceptions (`platform_http_panics_total`) and timeouts (`platform_http_request_timeouts_total`) on the same routes.
4. Check dependencies: `platform_dependency_request_seconds` by `dependency` and `outcome`.
5. Pull example failures from Loki: `{service="$service"} | json | level="error"`. Every entry carries `request_id` and `trace_id`; follow `trace_id` into Tempo.

**Mitigate.** Roll back a bad version, shed load, or fail over the failing dependency. Then confirm `service:platform_http_error_ratio:rate5m` drops below 0.0144.

## PlatformHTTPErrorBudgetSlowBurn

**Meaning.** A sustained 0.6% error ratio over both 6 hours and 30 minutes, which is 6× the budget.

**Triage.** As for the fast burn, but look for chronic causes: one degraded route, a slowly failing dependency, retry storms (`platform_retry_total`). Open a ticket for the owning team. This alert does not page.

## PlatformGRPCErrorBudgetFastBurn

**Meaning.** As for HTTP, over `platform_grpc_server_handled_total`. `status_class="5xx"` covers `Internal`, `Unavailable`, `Unknown`, `DataLoss`, `Unimplemented` and handler exceptions.

**Triage.** `sum by (grpc_method, grpc_code) (rate(platform_grpc_server_handled_total{service="$service",status_class="5xx"}[5m]))`, then the same version, panic and dependency checks as for HTTP.

## PlatformGRPCErrorBudgetSlowBurn

**Meaning and triage.** The gRPC counterpart of the HTTP slow burn.

## PlatformHTTPLatencyHigh

**Meaning.** The p99 latency of one route stayed above 1s for 10 minutes.

**Triage.**
1. Is it one route or all routes? Compare `service_route:platform_http_request_duration_seconds:p99_5m` across routes.
2. Saturation: `platform_http_active_requests` against replica count. Check HPA status.
3. Dependencies: `histogram_quantile(0.99, sum by (le, dependency) (rate(platform_dependency_request_seconds_bucket{service="$service"}[5m])))`.
4. Find slow spans in Tempo for the route.

**Mitigate.** Scale out, cache, or fix the slow dependency. If the 1s target is wrong for the route, override it in the service's own rules. Do not edit the platform rule.

## PlatformHTTPPanics

**Meaning.** A handler raised an exception nothing handled (fastapicommon counts these as `platform_http_panics_total`, the name gincommon uses for recovered panics). The request got a 500 `platform/internal-error` problem, the exception was logged, and the process kept running.

**Triage.** In Loki, find the `Unhandled exception while processing request` entries for `$service` (`|= "Unhandled exception"`). They carry the traceback, `request_id` and `trace_id`. Unhandled exceptions are bugs: open an issue against the service. They also count towards the error budget.

## PlatformGRPCPanics

**Meaning and triage.** As for HTTP, for `$grpc_method`: a handler exception without an explicit status (the client sees `UNKNOWN`). The span in Tempo carries the exception.

## PlatformUnclassifiedErrors

**Meaning:** a service answered at least one error with a generic internal error because nothing classified it. `platform_unclassified_errors_total{transport}` counts them; each one is a programming defect, not an outage.

**fastapicommon:** this version does not emit `platform_unclassified_errors_total` yet (it is in the registry since platform-gincommon v2.0.0; fastapicommon adds it together with its unclassified-error logging). Until then the alert does not fire for fastapicommon services: watch `PlatformHTTPPanics` / `PlatformGRPCPanics` instead, which count the same unhandled exceptions.

**Triage (once emitted):** find the failing code path in the logs, fix it where the failure happens by mapping the expected failure to a problem type (a `PlatformException`, a gRPC status), and treat a burst after a deploy as a new dependency error type the service does not classify yet.

## PlatformMessagesDeadLettered

**Meaning.** Messages from `$queue` were routed to the dead-letter queue in the last 10 minutes.

**Triage.**
1. Why: `sum by (reason, event_type) (increase(platform_dlq_messages_total{service="$service",queue="$queue"}[1h]))`.
2. What failed first: `platform_messages_failed_total` by `reason`, plus the consumer's error logs.
3. Backlog: `platform_dlq_depth{queue="$queue"}`.

**Mitigate.** Fix the cause, for example a schema mismatch, a missing tenant or a dependency outage. Then replay the DLQ using the queue's replay procedure. Never discard DLQ messages without the owning team's sign-off.

## PlatformMessagesFailureRatioHigh

**Meaning.** More than 5% of messages received from `$queue` failed processing for 10 minutes. Failures may still succeed on retry.

**Triage.** Break down `platform_messages_failed_total` by `reason` and `event_type`. Check `platform_retry_total` and the consumer's dependencies. A rise in `platform_duplicate_messages_total` at the same time points to redelivery after crashes.
