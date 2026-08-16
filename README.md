# Observability platform

Docker-managed central metrics platform for the internal laptop.

- **VictoriaMetrics** is the durable metrics database.
- **OpenTelemetry Collector** accepts OTLP metrics from APIs/services.
- **Grafana** is the dashboards and alerting UI.
- **Grafana Alloy** is the reusable edge agent for Linux servers.

## Network model

```text
home APIs ── OTLP ──> laptop:4318 ──> Collector ──> VictoriaMetrics
                                                      ^
prsmusa Alloy ─> 127.0.0.1:18428 ─ reverse SSH ────┘
```

The laptop initiates the SSH connection. Reverse ports bind to `127.0.0.1` on
the VPS, so VictoriaMetrics and OTLP are never public. Browser applications
must send telemetry to their own API/domain, not directly to this laptop.

## Start the central stack

```bash
cp .env.example .env
# Change the Grafana password in .env.
docker compose up -d
docker compose ps
```

Grafana defaults to laptop-only access on port 3000. OTLP is on internal-LAN
ports 4317/4318; restrict it to trusted hosts with the laptop firewall.

## Tunnel

```bash
sudo apt install autossh
sudo install -d -m 700 /etc/observability
sudo cp tunnel/tunnel.env.example /etc/observability/tunnel.env
sudoedit /etc/observability/tunnel.env
sudo cp tunnel/observability-reverse-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now observability-reverse-tunnel
```

On the VPS, `ss -ltn | grep 18428` should show only a loopback listener.

## VPS agent

Copy `edge/alloy/` to `prsmusa.com`, create `.env` containing:

```text
METRICS_REMOTE_WRITE_URL=http://127.0.0.1:18428/api/v1/write
```

Then run `docker compose up -d`. In Grafana Explore, query `up` or
`node_uname_info` after about one minute.
