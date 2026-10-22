#!/usr/bin/env bash
# Write Prometheus textfile metrics for NAS health (scraped via node-exporter).
#
# Failure-safe by design: a failed probe sets that family's *_valid gauge to 0 and
# omits its byte lines (never a fake 0), and the file is always rewritten when the
# script completes. Every call that touches the NFS mount or the network is bounded
# by `timeout`, because a hard NFS mount can otherwise hang forever.
# Backup size (du) is exported separately by nas-backup-usage-export.sh.
set -uo pipefail

CONFIG="${PRSM_NAS_ALERT_CONFIG:-$HOME/.config/prsm/nas-alert.env}"
if [[ -f "$CONFIG" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG"
fi

NAS_HOST="${NAS_HOST:-10.0.0.240}"
NAS_SSH="${NAS_SSH:-}"
SSH_IDENTITY="${NAS_SSH_IDENTITY:-$HOME/.ssh/prsm_nas_monitor}"
LOCAL_MOUNT="${LOCAL_MOUNT:-/mnt/production-backups}"
NAS_STORAGE_PATH="${NAS_STORAGE_PATH:-/mnt/storage}"
METRICS_DIR="${NAS_METRICS_DIR:-$HOME/metrics/nas-textfile}"
METRICS_FILE="${NAS_METRICS_FILE:-$METRICS_DIR/nas.prom}"
NFS_TIMEOUT="${NAS_NFS_TIMEOUT:-10}"
SSH_TIMEOUT="${NAS_SSH_TIMEOUT:-30}"

# Run a command that touches the NFS mount, killing it if it hangs.
bounded() { timeout -k 5 "$NFS_TIMEOUT" "$@"; }

# Parse `df -B1 -P` output into df_size/df_used/df_avail (Used column, not size-avail).
parse_df() {
  local line
  line="$(sed -n 2p <<<"$1")"
  read -r _ df_size df_used df_avail _ <<<"$line" || return 1
  [[ $df_size =~ ^[0-9]+$ && $df_used =~ ^[0-9]+$ && $df_avail =~ ^[0-9]+$ ]]
}

# Print the lines between "@@<name>" and the next "@@" marker.
section() { sed -n "/^@@$1\$/,/^@@/{/^@@/d;p}" <<<"$2"; }

reachable=0
mount_up=0
mount_writable=0
mount_df_valid=0
ssh_ok=0
# 0=unknown 1=healthy 2=degraded 3=ssh_check_failed
raid_status=0
storage_df_valid=0

if ping -c1 -W2 "$NAS_HOST" >/dev/null 2>&1; then
  reachable=1
fi

if bounded mountpoint -q "$LOCAL_MOUNT" 2>/dev/null; then
  mount_up=1
  probe="$LOCAL_MOUNT/.prsm-metrics-$$"
  if bounded touch "$probe" 2>/dev/null; then
    mount_writable=1
    bounded rm -f "$probe" 2>/dev/null || true
  fi
  if df_out="$(bounded df -B1 -P "$LOCAL_MOUNT" 2>/dev/null)" && parse_df "$df_out"; then
    mount_df_valid=1
    mount_size=$df_size mount_used=$df_used mount_avail=$df_avail
  fi
fi

if [[ -n "$NAS_SSH" ]]; then
  # One SSH call: RAID state plus df of the array root, in delimited sections.
  # Without root, mdadm usually fails and RAID state comes from /proc/mdstat alone.
  # shellcheck disable=SC2016  # expands on the NAS, not here
  remote_script='
echo "@@MDSTAT"; cat /proc/mdstat 2>/dev/null
echo "@@MDADM"; for d in /dev/md[0-9]*; do [ -e "$d" ] && mdadm --detail "$d" 2>/dev/null; done
echo "@@STORAGE"; if mountpoint -q "$1"; then df -B1 -P "$1"; else echo NOT_MOUNTED; fi
echo "@@END"'
  printf -v remote_path '%q' "$NAS_STORAGE_PATH"
  if out="$(timeout -k 5 "$SSH_TIMEOUT" ssh -o BatchMode=yes -o ConnectTimeout=8 \
      -o StrictHostKeyChecking=accept-new -i "$SSH_IDENTITY" \
      "$NAS_SSH" sh -s -- "$remote_path" <<<"$remote_script" 2>/dev/null)" \
      && grep -q '^@@END$' <<<"$out"; then
    ssh_ok=1
    raid_raw="$(section MDSTAT "$out")"$'\n'"$(section MDADM "$out")"
    # Not a bare "failed": mdadm --detail always prints "Failed Devices : 0".
    if grep -qiE 'degraded|Failed Devices : *[1-9]|faulty|\(F\)|recovery =|spare rebuilding' <<<"$raid_raw"; then
      raid_status=2
    elif grep -qE '\[[U_]*_[U_]*\]' <<<"$raid_raw"; then
      raid_status=2
    elif grep -qiE 'State : clean|active raid|raid1|raid5|raid6|raid10' <<<"$raid_raw"; then
      raid_status=1
    fi
    storage_out="$(section STORAGE "$out")"
    if [[ $storage_out != NOT_MOUNTED ]] && parse_df "$storage_out"; then
      storage_df_valid=1
      storage_size=$df_size storage_used=$df_used storage_avail=$df_avail
    fi
  else
    raid_status=3
  fi
fi

gauge() { printf '# HELP %s %s\n# TYPE %s gauge\n%s %s\n' "$1" "$2" "$1" "$1" "$3"; }

mkdir -p "$(dirname "$METRICS_FILE")"
# Temp name must not end in .prom: the textfile collector reads every *.prom file.
tmp="$(dirname "$METRICS_FILE")/.$(basename "$METRICS_FILE").tmp.$$"
trap 'rm -f "$tmp"' EXIT
{
  gauge prsm_nas_reachable "NAS host responds to ping (1=yes)." "$reachable"
  gauge prsm_nas_mount_up "Expected NFS backup mount is present on this host." "$mount_up"
  gauge prsm_nas_mount_writable "Backup mount passes a create/delete probe." "$mount_writable"
  gauge prsm_nas_mount_df_valid "df on the backup mount succeeded this run (byte gauges present only when 1)." "$mount_df_valid"
  if ((mount_df_valid)); then
    gauge prsm_nas_mount_size_bytes "Backup mount size, df 1B-blocks column (NFS client view of the filesystem)." "$mount_size"
    gauge prsm_nas_mount_used_bytes "Backup mount used, df Used column (NFS client view of the filesystem)." "$mount_used"
    gauge prsm_nas_mount_avail_bytes "Backup mount available, df Avail column (NFS client view of the filesystem)." "$mount_avail"
  fi
  gauge prsm_nas_ssh_ok "SSH check of the NAS succeeded this run." "$ssh_ok"
  gauge prsm_nas_raid_status "0=unknown 1=healthy 2=degraded 3=ssh_check_failed (from /proc/mdstat; mdadm detail when permitted)." "$raid_status"
  gauge prsm_nas_storage_df_valid "NAS array df succeeded this run: SSH ok, path mounted on the NAS, df parsed (byte gauges present only when 1)." "$storage_df_valid"
  if ((storage_df_valid)); then
    gauge prsm_nas_storage_size_bytes "NAS array size, df 1B-blocks column on NAS_STORAGE_PATH." "$storage_size"
    gauge prsm_nas_storage_used_bytes "NAS array used, df Used column on NAS_STORAGE_PATH." "$storage_used"
    gauge prsm_nas_storage_avail_bytes "NAS array available, df Avail column on NAS_STORAGE_PATH." "$storage_avail"
  fi
  gauge prsm_nas_check_timestamp_seconds "Unix time this export completed." "$(date +%s)"
} >"$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$METRICS_FILE"
