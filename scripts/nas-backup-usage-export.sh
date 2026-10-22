#!/usr/bin/env bash
# Write the NAS backup-usage textfile (scraped via node-exporter).
#
# Runs `du -s -B1` ON THE NAS over SSH (a local filesystem walk, far cheaper than
# walking the tree over NFS). du -B1 reports allocated disk usage, not apparent size.
# On failure: valid=0, the byte line is omitted, and the last-success timestamp is
# carried forward unchanged so staleness alerts and dashboard gates see the age.
# Sole writer of nas-backup-usage.prom; never touches nas.prom.
set -uo pipefail

CONFIG="${PRSM_NAS_ALERT_CONFIG:-$HOME/.config/prsm/nas-alert.env}"
if [[ -f "$CONFIG" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG"
fi

NAS_SSH="${NAS_SSH:-}"
SSH_IDENTITY="${NAS_SSH_IDENTITY:-$HOME/.ssh/prsm_nas_monitor}"
# Whitespace-separated paths on the NAS.
BACKUP_USAGE_PATHS="${BACKUP_USAGE_PATHS:-/mnt/storage/production-backups/baser4wm}"
METRICS_DIR="${NAS_METRICS_DIR:-$HOME/metrics/nas-textfile}"
METRICS_FILE="${NAS_BACKUP_METRICS_FILE:-$METRICS_DIR/nas-backup-usage.prom}"
DU_TIMEOUT="${NAS_DU_TIMEOUT:-540}"
SSH_TIMEOUT="${NAS_DU_SSH_TIMEOUT:-600}"

read -r -a paths <<<"$BACKUP_USAGE_PATHS"
valid=0
usage_bytes=0

if [[ -n "$NAS_SSH" ]] && ((${#paths[@]})); then
  # shellcheck disable=SC2016  # expands on the NAS, not here
  remote_script='
for p in "$@"; do [ -d "$p" ] || { echo "@@MISSING"; exit 3; }; done
timeout '"$DU_TIMEOUT"' du -s -B1 -- "$@"
echo "@@RC $?"'
  quoted=()
  for p in "${paths[@]}"; do
    printf -v q '%q' "$p"
    quoted+=("$q")
  done
  if out="$(timeout -k 10 "$SSH_TIMEOUT" ssh -o BatchMode=yes -o ConnectTimeout=8 \
      -o StrictHostKeyChecking=accept-new -i "$SSH_IDENTITY" \
      "$NAS_SSH" sh -s -- "${quoted[@]}" <<<"$remote_script")" \
      && grep -qx '@@RC 0' <<<"$out"; then
    lines=0
    sum=0
    ok=1
    while read -r bytes _; do
      [[ $bytes == @@* || -z $bytes ]] && continue
      if [[ $bytes =~ ^[0-9]+$ ]]; then
        sum=$((sum + bytes))
        lines=$((lines + 1))
      else
        ok=0
      fi
    done <<<"$out"
    if ((ok && lines == ${#paths[@]})); then
      valid=1
      usage_bytes=$sum
    fi
  fi
fi

if ((valid)); then
  last_success="$(date +%s)"
else
  # Carry forward the previous success time (absent if there never was one).
  last_success="$(awk '$1 == "prsm_nas_backup_usage_timestamp_seconds" {print $2}' "$METRICS_FILE" 2>/dev/null || true)"
fi

gauge() { printf '# HELP %s %s\n# TYPE %s gauge\n%s %s\n' "$1" "$2" "$1" "$1" "$3"; }

mkdir -p "$(dirname "$METRICS_FILE")"
# Temp name must not end in .prom: the textfile collector reads every *.prom file.
tmp="$(dirname "$METRICS_FILE")/.$(basename "$METRICS_FILE").tmp.$$"
trap 'rm -f "$tmp"' EXIT
{
  gauge prsm_nas_backup_usage_bytes_valid "Backup usage scan succeeded this run (byte gauge present only when 1)." "$valid"
  if ((valid)); then
    gauge prsm_nas_backup_usage_bytes "Allocated bytes under BACKUP_USAGE_PATHS on the NAS (du -s -B1 disk usage, not apparent size)." "$usage_bytes"
  fi
  if [[ $last_success =~ ^[0-9]+$ ]]; then
    gauge prsm_nas_backup_usage_timestamp_seconds "Unix time of the last successful backup usage scan." "$last_success"
  fi
} >"$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$METRICS_FILE"
