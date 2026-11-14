#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"

mode=${1:---check}
[[ $# -le 1 && ( $mode == --check || $mode == --plan ) ]] || fail 'Usage: bash deploy/scripts/preflight.sh [--check|--plan]'
configure
if [[ $mode == --plan ]]; then
  note 'Configuration parsed. Plan mode does not contact a daemon, registry or SSH host.'
  note 'Online checks will verify manager identity, node labels, secret existence, storage mount/UUID, cached image platforms and existing database settings.'
else
  online_preflight
fi
