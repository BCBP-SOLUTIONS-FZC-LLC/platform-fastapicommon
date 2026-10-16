global:
  scrape_interval: 5s

# Reference recording rules and SLO burn-rate alerts, vendored from platform-gincommon
# (docker/prometheus/rules; `make metrics-rules` checks them with promtool).
rule_files:
  - /etc/prometheus/rules/platform-*.yml

scrape_configs:
  - job_name: "services"
    metrics_path: /metrics
    static_configs:
      - targets: ["__PROMETHEUS_SCRAPE_TARGET__"]
