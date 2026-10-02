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

### prsmusa.com dashboard route

The deployed dashboard URL is `https://prsmusa.com/metrics/`. Grafana is
configured with that URL as its root and serves the `/metrics/` subpath
directly. The internal host forwards only `127.0.0.1:3000` to VPS loopback
`127.0.0.1:13000`; VictoriaMetrics remains private.

- Internal tunnel loop: `tunnel/prsmusa-metrics-tunnel`
- Nginx locations: `tunnel/nginx-prsmusa-metrics.conf`
- Tunnel startup follows the existing host convention:
  `@reboot /usr/bin/flock -n /home/baser4wm/.cache/prsmusa-metrics-tunnel.lock /home/baser4wm/bin/prsmusa-metrics-tunnel`

Use a dedicated SSH key constrained on the VPS to
`permitlisten="127.0.0.1:13000"`. Validate Nginx with `nginx -t` before reload.

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

Grafana provisions **Platform Overview** and **Bible API** dashboards. Platform
Overview includes osTicket's latest backup result, backup age, repository space,
and firing backup alerts. osTicket failures, missing or older-than-26-hour
backups, and failed integrity checks appear in `ALERTS`. Backup service logs
retain operational failures.
Alertmanager's `discard` receiver remains enabled; no external notifications
are sent.

The osTicket backup service writes `/home/baser4wm/osticket/metrics/backup.prom`
atomically. Set `BACKUP_METRICS_FILE` to that path in osTicket's environment.
Create the metrics directory with mode `0755` and metrics files with `0644`;
the containing osTicket data directory stays private (`0700`). Node-exporter
mounts only the metrics directory directly, so it can read the metrics without
access to credentials or archives. `OSTICKET_METRICS_DIR` may override the host
path in this stack's environment. The bind requires the directory to exist
before starting node-exporter.

Validate backup alert failure, success, missing metrics, and the 26-hour
boundary without sending notifications:

```bash
docker run --rm --entrypoint /bin/promtool -v "$PWD:/work:ro" -w /work \
  prom/prometheus:v3.7.3 test rules tests/osticket-rules.yaml
```
