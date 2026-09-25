#!/usr/bin/env bash
# Render the Grafana alerting files that depend on the optional external
# heartbeat. Usage: render-heartbeat.sh <alerting-dir> <heartbeat-url-or-empty>
#
# Only the presence of the URL is used here; the URL itself reaches Grafana
# through its container environment, so it never lands in a rendered file.
# Empty -> heartbeat.yml is removed, policies.yml carries no heartbeat route,
# and the always-firing watchdog rule does not exist. Never fails on empty.
set -euo pipefail

dir="${1:?alerting dir}"
url="${2:-}"

if [[ -n "${url}" ]]; then
  cp "${dir}/heartbeat.yml.tmpl" "${dir}/heartbeat.yml"
  route='    routes:
      - receiver: external-heartbeat
        object_matchers:
          - ["heartbeat", "=", "external"]
        group_by: ["alertname"]
        group_wait: 0s
        group_interval: 1m
        repeat_interval: 5m'
else
  rm -f "${dir}/heartbeat.yml"
  route=''
fi

while IFS= read -r line || [[ -n "${line}" ]]; do
  if [[ "${line}" == "__HEARTBEAT_ROUTES__" ]]; then
    [[ -z "${route}" ]] || printf '%s\n' "${route}"
  else
    printf '%s\n' "${line}"
  fi
done < "${dir}/policies.yml.tmpl" > "${dir}/policies.yml"
