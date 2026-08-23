#!/usr/bin/env bash
# Minimal workspace-container keeper. Podman's --init is PID 1; this script
# is its PID 2 child. It exits only after three consecutive idle checks, which
# lets Podman's --rm remove the transient workspace box once no agent or
# daemon process remains. The scan deliberately uses shell builtins and /proc
# so its own inspection does not create a process that looks like workload.
set -u

idle_checks=0
while :; do
  workloads=0
  for proc in /proc/[0-9]*; do
    pid=${proc##*/}
    (( pid <= 2 )) && continue
    [[ -d "$proc" ]] || continue
    workloads=1
    break
  done

  if (( workloads )); then
    idle_checks=0
  else
    ((idle_checks += 1))
    (( idle_checks >= 3 )) && exit 0
  fi
  sleep 1
done
