#!/usr/bin/env python3
"""Verify node-exporter reads the textfile binds (Kaneo beneath a private parent, NAS),
and ignores in-progress temp files that do not end in .prom."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

with tempfile.TemporaryDirectory(prefix="textfile-metrics-") as private:
    os.chmod(private, 0o700)
    nas = Path(private) / "nas"
    nas.mkdir(mode=0o755)
    (nas / "nas.prom").write_text("prsm_nas_reachable 1\n")
    (nas / "nas.prom").chmod(0o644)
    # Exporters write ".<name>.prom.tmp.<pid>" before an atomic mv; it must be ignored.
    (nas / ".nas-backup-usage.prom.tmp.123").write_text("prsm_nas_backup_usage_bytes 42\n")
    (nas / ".nas-backup-usage.prom.tmp.123").chmod(0o644)
    kaneo = Path(private) / "kaneo"
    kaneo.mkdir(mode=0o755)
    (kaneo / "health.prom").write_text("kaneo_application_up 1\n")
    (kaneo / "health.prom").chmod(0o644)
    container = subprocess.check_output([
        "docker", "run", "--rm", "-d", "--user", "65534:65534",
        "-p", "127.0.0.1::9100", "-v", f"{kaneo}:/kaneo-textfile:ro",
        "-v", f"{nas}:/nas-textfile:ro",
        "prom/node-exporter:v1.9.1", "--collector.textfile.directory=/kaneo-textfile",
        "--collector.textfile.directory=/nas-textfile",
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
        assert "prsm_nas_reachable 1\n" in body
        assert "prsm_nas_backup_usage_bytes" not in body, "temp files must be ignored"
        assert "kaneo_application_up 1\n" in body
        assert "node_textfile_scrape_error 0\n" in body
        print("Textfile scrape passed (Kaneo + NAS, temp files ignored)")
    finally:
        subprocess.run(["docker", "stop", container], check=True, stdout=subprocess.DEVNULL)
