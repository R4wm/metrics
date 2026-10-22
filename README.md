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
Overview shows host CPU, memory, and disk; Kaneo health and backups; and NAS
health, backup growth, and array capacity. Alertmanager's `discard` receiver
remains enabled; no external notifications are sent.

Run the checks (rule tests need Docker):

```bash
python3 tests/test-nas-export.py
python3 tests/test-textfile-exporter.py
docker run --rm --entrypoint /bin/promtool -v "$PWD:/work:ro" -w /work \
  prom/prometheus:v3.7.3 test rules tests/nas-rules.yaml tests/kaneo-rules.yaml
```

### NAS storage (LAN `10.0.0.240`)

Two user timers on baser4wm write textfiles in `~/metrics/nas-textfile/`, scraped
by node-exporter. Each script is the **sole writer** of its file and replaces it
atomically (temp name `.<file>.tmp.<pid>`, which the collector ignores).

| File | Script / timer | Cadence | What it measures |
| --- | --- | --- | --- |
| `nas.prom` | `scripts/nas-export-metrics.sh` / `nas-metrics-export` | 1 min | Ping, NFS mount up/writable, mount `df` (client view), and over SSH: RAID state and `df` of the NAS array (`NAS_STORAGE_PATH`) |
| `nas-backup-usage.prom` | `scripts/nas-backup-usage-export.sh` / `nas-backup-usage-export` | 15 min | `du -s -B1` **on the NAS over SSH** for `BACKUP_USAGE_PATHS` |

**Semantics**

- **df vs du:** `prsm_nas_storage_*_bytes` and `prsm_nas_mount_*_bytes` are
  filesystem blocks from the `df` **Used/Avail** columns (the whole filesystem,
  not just backups; the NFS mount may report the same pool as the NAS array).
  `prsm_nas_backup_usage_bytes` is **allocated** disk usage of the backup trees
  (`du -B1`, not apparent size).
- **Failures are gaps, never zeros:** a failed measurement sets that family's
  `*_valid` gauge to 0 and omits its byte lines. Dashboard panels gate on
  `*_valid == 1` and on freshness of `prsm_nas_check_timestamp_seconds` (< 180 s)
  or `prsm_nas_backup_usage_timestamp_seconds` (< 2700 s).
- **Crashes show as staleness:** a killed or timed-out run leaves the previous
  file unchanged; its timestamp stops advancing. The backup script carries the
  last *successful* scan time forward on failure. `NasMetricsStale` and
  `NasBackupUsageStale` alert on age.
- **Bounded runtime:** every NFS call runs under `timeout` (`NAS_NFS_TIMEOUT`,
  default 10 s) and SSH under `NAS_SSH_TIMEOUT` (30 s); the remote `du` is capped
  at 540 s. Units set `TimeoutStartSec` (90 s fast, 660 s backup).
- **RAID:** from `/proc/mdstat`, plus `mdadm --detail` when the monitor user is
  permitted. `3` means the SSH check failed.

**Config** (`~/.config/prsm/nas-alert.env`, shared with
[PRSM nas-alert](https://github.com/R4wm/prsm/blob/main/docs/ops-nas-storage-alerts.md)):
`NAS_HOST`, `NAS_SSH` (e.g. `r4wm@10.0.0.240`; unset disables SSH checks),
`NAS_SSH_IDENTITY` (default `~/.ssh/prsm_nas_monitor`), `LOCAL_MOUNT`,
`NAS_STORAGE_PATH` (default `/mnt/storage`), `BACKUP_USAGE_PATHS`
(whitespace-separated NAS paths), `NAS_METRICS_DIR`.

**Install / update on baser4wm**

```bash
cd ~/github/metrics && git pull
chmod +x scripts/nas-export-metrics.sh scripts/nas-backup-usage-export.sh
mkdir -p ~/metrics/nas-textfile
cp systemd/nas-metrics-export.* systemd/nas-backup-usage-export.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nas-metrics-export.timer nas-backup-usage-export.timer
loginctl enable-linger baser4wm   # timers run without a login session
scripts/nas-export-metrics.sh && scripts/nas-backup-usage-export.sh
cat ~/metrics/nas-textfile/*.prom
docker compose up -d node-exporter
docker compose restart vmalert grafana   # rules and dashboards are not hot-reloaded
```

vmalert rules: `central/vmalert/nas.yaml`.
