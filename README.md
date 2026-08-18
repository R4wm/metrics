# Metrics platform

Docker-managed observability for services on `10.0.0.68`.

## Components

- **VictoriaMetrics** stores Prometheus-compatible metrics for 90 days.
- **Grafana** provides the private dashboard and alert UI.
- **Alloy** discovers opted-in Docker containers and scrapes their Prometheus metrics.
- **node_exporter** and **cAdvisor** provide host and Docker-container health metrics.
- **OpenTelemetry Collector** accepts OTLP metrics from services connected to the
  shared `metrics_internal` Docker network.
- **vmalert** evaluates alert rules and sends them to Alertmanager. The initial
  receiver intentionally discards notifications until Slack, email, or a webhook
  is configured.

## Security model

- Grafana and VictoriaMetrics bind only to `127.0.0.1` on `10.0.0.68`.
- VictoriaMetrics, Alertmanager, exporters, and the Collector have no public
  host ports. Expose Grafana only through the existing reverse-SSH/Nginx route.
- Alloy and cAdvisor mount the Docker socket/read host files to observe the host.
  Treat access to this repository and the Docker host as privileged.

## Deploy on 10.0.0.68

```bash
git clone ssh://git@github.com/R4wm/metrics.git ~/github/metrics
cd ~/github/metrics
cp .env.example .env
# Set GRAFANA_ADMIN_PASSWORD to a strong, unique value.
./scripts/deploy.sh
```

The deploy user must be in the `docker` group or run the script with `sudo`.
After startup, Grafana listens on `127.0.0.1:3000`; VictoriaMetrics listens on
`127.0.0.1:8428`. Configure the existing reverse SSH tunnel/Nginx proxy to
forward only Grafana's loopback port. The reusable systemd template is in
`tunnel/observability-reverse-tunnel.service`; set its destination and remote
port in `/etc/observability/tunnel.env`.

## Add a Docker service

Expose a Prometheus endpoint and opt the service into scraping:

```yaml
services:
  example-api:
    labels:
      metrics.scrape: "true"
      metrics.port: "8080"
      metrics.path: "/metrics"
      metrics.job: "example_api"
      metrics.service: "example-api"
```

Alloy discovers the container every 30 seconds and adds stable `job`, `service`,
`host`, and Compose-project labels. Never put user IDs, IP addresses, raw URLs,
query strings, email addresses, or secrets in metric labels.

For OTLP metrics, add the service to the external `metrics_internal` network and
send to `http://otel-collector:4318`. Prometheus scraping is the default for
local Docker services.

## Validate

```bash
docker compose ps
curl -fsS http://127.0.0.1:8428/health
curl -fsS 'http://127.0.0.1:8428/api/v1/query?query=up'
```

Grafana provisions **Platform Overview** and **Bible API** dashboards. Alert
rules can be queried with `ALERTS`; they do not send external notifications yet.
