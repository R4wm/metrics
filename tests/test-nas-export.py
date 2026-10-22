#!/usr/bin/env python3
"""Scenario tests for the NAS textfile exporters, using fake system commands.

Fakes for ping/mountpoint/df/touch/ssh are put first on PATH (the real `timeout`
finds them too), driven by FAKE_* environment variables. No NAS or NFS needed.

Run: python3 tests/test-nas-export.py
"""
import os
import signal
import stat
import subprocess
import tempfile
import textwrap
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAST = REPO / "scripts" / "nas-export-metrics.sh"
BACKUP = REPO / "scripts" / "nas-backup-usage-export.sh"

FAKES = {
    "ping": 'exit "${FAKE_PING:-0}"',
    "mountpoint": '[ "${FAKE_MOUNTPOINT:-0}" = hang ] && sleep 60; exit "${FAKE_MOUNTPOINT:-0}"',
    "touch": '[ "${FAKE_TOUCH:-ok}" = fail ] && exit 1; exec /usr/bin/touch "$@"',
    "df": textwrap.dedent("""\
        case "${FAKE_DF:-ok}" in
          ok) printf 'Filesystem 1B-blocks Used Available Capacity Mounted on\\nnas:/exp 1000 600 350 64%% /mnt\\n';;
          empty) exit 0;;
          fail) exit 1;;
          hang) sleep 60;;
        esac"""),
    "ssh": textwrap.dedent("""\
        if [ -n "$FAKE_SSH_STDIN" ]; then cat >"$FAKE_SSH_STDIN"; else cat >/dev/null; fi
        case "${FAKE_SSH:-ok}" in
          ok) cat "$FAKE_SSH_OUT";;
          fail) exit 255;;
          hang) sleep 60;;
        esac"""),
}

HEALTHY_REMOTE = """\
@@MDSTAT
Personalities : [raid1]
md0 : active raid1 sdb1[1] sda1[0]
      976630336 blocks super 1.2 [2/2] [UU]
@@MDADM
           State : clean
  Failed Devices : 0
@@STORAGE
Filesystem 1B-blocks Used Available Capacity Mounted on
/dev/md0 8000 5000 2600 66% /mnt/storage
@@END
"""


def prom(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if line and not line.startswith("#"):
            name, value = line.split()
            values[name] = float(value)
    return values


class Env:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nas-export-test-")
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, body in FAKES.items():
            f = self.bin / name
            f.write_text("#!/bin/sh\n" + body + "\n")
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.metrics = root / "metrics"
        self.mount = root / "mount"
        self.mount.mkdir()
        self.ssh_out = root / "ssh_out"
        self.ssh_out.write_text(HEALTHY_REMOTE)
        self.base = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(root),
            "PRSM_NAS_ALERT_CONFIG": str(root / "missing.env"),
            "NAS_METRICS_DIR": str(self.metrics),
            "LOCAL_MOUNT": str(self.mount),
            "NAS_SSH": "monitor@nas.test",
            "NAS_SSH_IDENTITY": str(root / "key"),
            "BACKUP_USAGE_PATHS": "/a /b",
            "NAS_NFS_TIMEOUT": "1",
            "NAS_SSH_TIMEOUT": "2",
            "NAS_DU_SSH_TIMEOUT": "2",
            "FAKE_SSH_OUT": str(self.ssh_out),
        }

    def run(self, script, timeout=30, **fake):
        env = {**self.base, **{k: str(v) for k, v in fake.items()}}
        start = time.time()
        subprocess.run(["bash", str(script)], env=env, check=True, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return time.time() - start

    @property
    def fast(self):
        return prom(self.metrics / "nas.prom")

    @property
    def backup(self):
        return prom(self.metrics / "nas-backup-usage.prom")

    def leftovers(self):
        return sorted(p.name for p in self.metrics.iterdir()
                      if p.name not in ("nas.prom", "nas-backup-usage.prom"))


TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@test
def happy_path_all_families_valid():
    e = Env()
    e.run(FAST)
    m = e.fast
    assert m["prsm_nas_reachable"] == 1 and m["prsm_nas_mount_up"] == 1 and m["prsm_nas_mount_writable"] == 1
    assert m["prsm_nas_mount_df_valid"] == 1
    assert (m["prsm_nas_mount_size_bytes"], m["prsm_nas_mount_used_bytes"], m["prsm_nas_mount_avail_bytes"]) == (1000, 600, 350), \
        "used must come from the df Used column, not size-avail (650)"
    assert m["prsm_nas_ssh_ok"] == 1 and m["prsm_nas_raid_status"] == 1, "Failed Devices : 0 must not read as degraded"
    assert m["prsm_nas_storage_df_valid"] == 1 and m["prsm_nas_storage_used_bytes"] == 5000
    assert abs(m["prsm_nas_check_timestamp_seconds"] - time.time()) < 60
    assert not any(k.startswith("prsm_nas_backup") for k in m), "fast file must not carry backup metrics"
    assert e.leftovers() == []


@test
def local_df_prints_nothing_still_writes_file():
    # Regression: under `set -e`, `read < <(df ...)` aborted the script and left a stale file.
    e = Env()
    e.run(FAST, FAKE_DF="empty")
    m = e.fast
    assert m["prsm_nas_mount_up"] == 1 and m["prsm_nas_mount_df_valid"] == 0
    assert "prsm_nas_mount_avail_bytes" not in m, "no fake zeros: byte lines omitted"
    assert m["prsm_nas_storage_df_valid"] == 1, "other families unaffected"


@test
def local_df_fails():
    e = Env()
    e.run(FAST, FAKE_DF="fail")
    assert e.fast["prsm_nas_mount_df_valid"] == 0 and "prsm_nas_mount_size_bytes" not in e.fast


@test
def nfs_hang_is_bounded():
    e = Env()
    took = e.run(FAST, FAKE_MOUNTPOINT="hang")
    assert took < 15, f"hung NFS call must be killed by timeout (took {took:.1f}s)"
    m = e.fast
    assert m["prsm_nas_mount_up"] == 0 and m["prsm_nas_mount_df_valid"] == 0


@test
def nfs_df_hang_is_bounded():
    e = Env()
    took = e.run(FAST, FAKE_DF="hang")
    assert took < 15
    assert e.fast["prsm_nas_mount_df_valid"] == 0


@test
def mount_not_writable():
    e = Env()
    e.run(FAST, FAKE_TOUCH="fail")
    assert e.fast["prsm_nas_mount_writable"] == 0 and e.fast["prsm_nas_mount_df_valid"] == 1


@test
def ssh_fails():
    e = Env()
    e.run(FAST, FAKE_SSH="fail")
    e.run(BACKUP, FAKE_SSH="fail")
    m, b = e.fast, e.backup
    assert m["prsm_nas_ssh_ok"] == 0 and m["prsm_nas_raid_status"] == 3
    assert m["prsm_nas_storage_df_valid"] == 0 and "prsm_nas_storage_used_bytes" not in m
    assert m["prsm_nas_mount_df_valid"] == 1, "local mount stats unaffected by SSH failure"
    assert b["prsm_nas_backup_usage_bytes_valid"] == 0 and "prsm_nas_backup_usage_bytes" not in b
    assert "prsm_nas_backup_usage_timestamp_seconds" not in b, "never succeeded: no timestamp"


@test
def ssh_hang_is_bounded():
    e = Env()
    took = e.run(FAST, FAKE_SSH="hang")
    assert took < 15 and e.fast["prsm_nas_ssh_ok"] == 0


@test
def remote_array_not_mounted():
    e = Env()
    e.ssh_out.write_text(HEALTHY_REMOTE.replace(
        "Filesystem 1B-blocks Used Available Capacity Mounted on\n/dev/md0 8000 5000 2600 66% /mnt/storage\n",
        "NOT_MOUNTED\n"))
    e.run(FAST)
    m = e.fast
    assert m["prsm_nas_ssh_ok"] == 1 and m["prsm_nas_storage_df_valid"] == 0
    assert "prsm_nas_storage_size_bytes" not in m


@test
def raid_degraded():
    e = Env()
    e.ssh_out.write_text(HEALTHY_REMOTE.replace("[2/2] [UU]", "[2/1] [U_]"))
    e.run(FAST)
    assert e.fast["prsm_nas_raid_status"] == 2


@test
def no_nas_ssh_configured():
    e = Env()
    e.base["NAS_SSH"] = ""
    e.run(FAST)
    e.run(BACKUP)
    assert e.fast["prsm_nas_ssh_ok"] == 0 and e.fast["prsm_nas_raid_status"] == 0
    assert e.backup["prsm_nas_backup_usage_bytes_valid"] == 0


@test
def backup_happy_path_sums_paths():
    e = Env()
    e.ssh_out.write_text("4096\t/a\n8192\t/b\n@@RC 0\n")
    e.run(BACKUP)
    b = e.backup
    assert b["prsm_nas_backup_usage_bytes_valid"] == 1 and b["prsm_nas_backup_usage_bytes"] == 12288
    assert abs(b["prsm_nas_backup_usage_timestamp_seconds"] - time.time()) < 60
    assert e.leftovers() == []


@test
def backup_du_runs_via_sudo_user_when_configured():
    e = Env()
    e.ssh_out.write_text("4096\t/a\n8192\t/b\n@@RC 0\n")
    sent = Path(e.tmp.name) / "remote_script"
    e.run(BACKUP, FAKE_SSH_STDIN=sent)
    assert "sudo" not in sent.read_text(), "no sudo unless configured"
    e.base["NAS_DU_SUDO_USER"] = "osticket-backup"
    e.run(BACKUP, FAKE_SSH_STDIN=sent)
    assert "timeout 540 sudo -n -u osticket-backup du -s -B1 --" in sent.read_text()
    assert e.backup["prsm_nas_backup_usage_bytes_valid"] == 1


@test
def backup_du_failure_keeps_last_success_timestamp():
    e = Env()
    e.ssh_out.write_text("4096\t/a\n8192\t/b\n@@RC 0\n")
    e.run(BACKUP)
    first = e.backup["prsm_nas_backup_usage_timestamp_seconds"]
    e.run(FAST)
    fast_before = (e.metrics / "nas.prom").read_text()
    time.sleep(1.1)
    e.ssh_out.write_text("4096\t/a\n@@RC 1\n")  # du: permission denied on /b
    e.run(BACKUP)
    b = e.backup
    assert b["prsm_nas_backup_usage_bytes_valid"] == 0 and "prsm_nas_backup_usage_bytes" not in b
    assert b["prsm_nas_backup_usage_timestamp_seconds"] == first, "timestamp must not advance on failure"
    assert (e.metrics / "nas.prom").read_text() == fast_before, "backup script must never touch nas.prom"


@test
def backup_missing_path_or_partial_output_is_invalid():
    e = Env()
    e.ssh_out.write_text("@@MISSING\n")
    e.run(BACKUP)
    assert e.backup["prsm_nas_backup_usage_bytes_valid"] == 0
    e.ssh_out.write_text("4096\t/a\n@@RC 0\n")  # one line for two paths
    e.run(BACKUP)
    assert e.backup["prsm_nas_backup_usage_bytes_valid"] == 0


@test
def concurrent_writers_do_not_clobber():
    e = Env()
    e.ssh_out.write_text(HEALTHY_REMOTE)
    errors = []

    def go(script, **fake):
        try:
            e.run(script, **fake)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(FAST,)) for _ in range(3)]
    threads += [threading.Thread(target=go, args=(BACKUP,)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert not any(k.startswith("prsm_nas_backup") for k in e.fast)
    assert all(k.startswith("prsm_nas_backup") for k in e.backup)
    assert e.leftovers() == []


@test
def killed_run_leaves_previous_file_unchanged():
    e = Env()
    e.run(FAST)
    before = (e.metrics / "nas.prom").read_text()
    env = {**e.base, "FAKE_SSH": "hang", "NAS_SSH_TIMEOUT": "30"}
    p = subprocess.Popen(["bash", str(FAST)], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)
    os.killpg(p.pid, signal.SIGKILL)
    p.wait()
    assert (e.metrics / "nas.prom").read_text() == before, "crash must not rewrite the file"
    assert not any(n.endswith(".prom") for n in e.leftovers()), "temp files must not end in .prom"


def main():
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"[ok] {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {fn.__name__}: {exc!r}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
