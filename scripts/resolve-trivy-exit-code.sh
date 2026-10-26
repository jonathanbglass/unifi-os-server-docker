#!/usr/bin/env bash
# Print 0 or 1 for the Trivy exit code.
# workflow_dispatch input fail_on_vulnerabilities=true forces 1.
# Otherwise the repository variable TRIVY_EXIT_CODE is used when it is 0 or 1.

set -euo pipefail

event_name="${EVENT_NAME:-}"
input_fail="${INPUT_FAIL:-}"
variable="${VAR_TRIVY:-}"

if [[ "$event_name" == "workflow_dispatch" && "$input_fail" == "true" ]]; then
  printf '1\n'
  exit 0
fi

if [[ -n "$variable" ]]; then
  case "$variable" in
    0|1)
      printf '%s\n' "$variable"
      ;;
    *)
      printf 'TRIVY_EXIT_CODE must be 0 or 1\n' >&2
      exit 1
      ;;
  esac
  exit 0
fi

printf '0\n'
