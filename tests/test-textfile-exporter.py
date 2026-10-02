#!/usr/bin/env python3
"""Verify nobody can scrape the explicit metrics bind beneath a private parent."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

with tempfile.TemporaryDirectory(prefix="osticket-metrics-") as private:
    os.chmod(private, 0o700)
    metrics = Path(private) / "metrics"
    metrics.mkdir(mode=0o755)
    metric = metrics / "backup.prom"
    metric.write_text("osticket_backup_last_attempt_success 1\n")
    metric.chmod(0o644)
    container = subprocess.check_output([
        "docker", "run", "--rm", "-d", "--user", "65534:65534",
        "-p", "127.0.0.1::9100", "-v", f"{metrics}:/textfile:ro",
        "prom/node-exporter:v1.9.1", "--collector.textfile.directory=/textfile",
    ], text=True).strip()
    try:
        address = subprocess.check_output(
            ["docker", "port", container, "9100"], text=True
        ).strip()
        for attempt in range(30):
            try:
                with urllib.request.urlopen(f"http://{address}/metrics", timeout=2) as response:
                    body = response.read().decode()
                break
            except OSError:
                if attempt == 29:
                    raise
                time.sleep(0.2)
        assert "osticket_backup_last_attempt_success 1\n" in body
        assert "node_textfile_scrape_error 0\n" in body
        print("Private-parent textfile scrape passed")
    finally:
        subprocess.run(["docker", "stop", container], check=True, stdout=subprocess.DEVNULL)
