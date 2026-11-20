#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"

mount=${POSTGRES_VOLUME_MOUNT:?Set POSTGRES_VOLUME_MOUNT}
uuid=${POSTGRES_VOLUME_UUID:?Set POSTGRES_VOLUME_UUID}
threshold=${POSTGRES_VOLUME_ALERT_PERCENT:-80}
[[ $mount == /* && $mount != / && $threshold =~ ^([1-9][0-9]?|100)$ ]] || fail 'Invalid Volume alert settings'
actual=$(findmnt -nr -o UUID --mountpoint "$mount") && [[ $actual == "$uuid" ]] ||
  fail 'PostgreSQL Volume missing or filesystem UUID changed'
options=$(findmnt -nr -o OPTIONS --mountpoint "$mount")
[[ ,$options, == *,rw,* ]] || fail 'PostgreSQL Volume is read-only'
percent=$(LC_ALL=C df -P -- "$mount" | awk 'NR == 2 && $5 ~ /^[0-9]+%$/ {sub(/%$/, "", $5); print $5}')
[[ $percent =~ ^[0-9]+$ ]] || fail 'Cannot measure PostgreSQL Volume utilization'
(( percent < threshold )) || fail "PostgreSQL Volume usage is $percent% (threshold $threshold%)"
