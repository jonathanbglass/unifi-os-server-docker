#!/usr/bin/env bash
# Print true when this GitHub Actions event should push to GHCR.
# Pull requests never publish. workflow_dispatch publishes unless push=false.

set -euo pipefail

event_name="${EVENT_NAME:-}"
input_push="${INPUT_PUSH:-}"

if [[ "$event_name" == "pull_request" ]]; then
  printf 'false\n'
  exit 0
fi

if [[ "$event_name" == "workflow_dispatch" && "$input_push" == "false" ]]; then
  printf 'false\n'
  exit 0
fi

printf 'true\n'
