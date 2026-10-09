#!/usr/bin/env bash
# Write Prometheus textfile metrics for NAS health (scraped via node-exporter).
set -euo pipefail

CONFIG="${PRSM_NAS_ALERT_CONFIG:-$HOME/.config/prsm/nas-alert.env}"
if [[ -f "$CONFIG" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG"
fi

NAS_HOST="${NAS_HOST:-10.0.0.240}"
LOCAL_MOUNT="${LOCAL_MOUNT:-/mnt/production-backups}"
METRICS_DIR="${NAS_METRICS_DIR:-$HOME/metrics/nas-textfile}"
METRICS_FILE="${NAS_METRICS_FILE:-$METRICS_DIR/nas.prom}"
SSH_IDENTITY="${NAS_SSH_IDENTITY:-$HOME/.ssh/prsm_nas_monitor}"

mkdir -p "$METRICS_DIR"
tmp="${METRICS_FILE}.$$"

reachable=0
mount_up=0
mount_writable=0
avail_bytes=0
size_bytes=0
# 0=unknown 1=healthy 2=degraded 3=check_failed
raid_status=0
ssh_ok=0

if ping -c1 -W2 "$NAS_HOST" >/dev/null 2>&1; then
  reachable=1
fi

if mountpoint -q "$LOCAL_MOUNT" 2>/dev/null; then
  mount_up=1
  probe="$LOCAL_MOUNT/.prsm-metrics-$$"
  if touch "$probe" 2>/dev/null; then
    mount_writable=1
    rm -f "$probe"
  fi
  read -r size_bytes avail_bytes _ < <(df -B1 -P "$LOCAL_MOUNT" 2>/dev/null | awk 'NR==2 {print $2, $4}')
fi

if [[ -n "${NAS_SSH:-}" ]] && [[ -f "$SSH_IDENTITY" ]]; then
  if raid_raw="$(ssh -o BatchMode=yes -o ConnectTimeout=8 -i "$SSH_IDENTITY" -o StrictHostKeyChecking=accept-new \
    "$NAS_SSH" 'cat /proc/mdstat 2>/dev/null; mdadm --detail --scan 2>/dev/null | while read -r _ d _; do mdadm --detail "$d" 2>/dev/null; done' 2>/dev/null)"; then
    ssh_ok=1
    if grep -qiE 'degraded|State.*degraded|failed|Faulty|spare rebuilding' <<<"$raid_raw"; then
      raid_status=2
    elif grep -E '\[.*_.*\]' <<<"$raid_raw" >/dev/null; then
      raid_status=2
    elif grep -qiE 'State : clean|active raid|raid1|raid5|raid6|raid10' <<<"$raid_raw"; then
      raid_status=1
    else
      raid_status=0
    fi
  else
    raid_status=3
  fi
elif [[ -f "$SSH_IDENTITY" ]]; then
  if ssh -o BatchMode=yes -o ConnectTimeout=8 -i "$SSH_IDENTITY" "rmintz@${NAS_HOST}" 'cat /proc/mdstat' >/dev/null 2>&1; then
    ssh_ok=1
    raid_status=0
  fi
fi

now="$(date +%s)"
cat >"$tmp" <<EOF
# HELP prsm_nas_reachable NAS host responds to ping (1=yes).
# TYPE prsm_nas_reachable gauge
prsm_nas_reachable $reachable
# HELP prsm_nas_mount_up Expected NFS/CIFS mount is present.
# TYPE prsm_nas_mount_up gauge
prsm_nas_mount_up $mount_up
# HELP prsm_nas_mount_writable Mount passes create/delete probe.
# TYPE prsm_nas_mount_writable gauge
prsm_nas_mount_writable $mount_writable
# HELP prsm_nas_mount_avail_bytes Available bytes on LOCAL_MOUNT.
# TYPE prsm_nas_mount_avail_bytes gauge
prsm_nas_mount_avail_bytes ${avail_bytes:-0}
# HELP prsm_nas_mount_size_bytes Total bytes on LOCAL_MOUNT.
# TYPE prsm_nas_mount_size_bytes gauge
prsm_nas_mount_size_bytes ${size_bytes:-0}
# HELP prsm_nas_ssh_ok SSH to NAS for RAID detail succeeded.
# TYPE prsm_nas_ssh_ok gauge
prsm_nas_ssh_ok $ssh_ok
# HELP prsm_nas_raid_status 0=unknown 1=healthy 2=degraded 3=ssh_check_failed.
# TYPE prsm_nas_raid_status gauge
prsm_nas_raid_status $raid_status
# HELP prsm_nas_check_timestamp_seconds Unix time of last export.
# TYPE prsm_nas_check_timestamp_seconds gauge
prsm_nas_check_timestamp_seconds $now
EOF
chmod 644 "$tmp"
mv -f "$tmp" "$METRICS_FILE"
