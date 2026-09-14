#!/usr/bin/env bash
# Minimal workspace-container keeper. Podman's --init is PID 1; this script
# is its PID 2 child. Its final idle decision takes the exact flock used by
# host-side exec admission, so a new admitted workload and idle shutdown are
# mutually exclusive.
set -u

lock_path=${1:?agent-box keeper requires a lock path}
# `flock` locks the open file description inherited from this shell.  This is
# the usual fd-lock idiom: the external `flock` command returns, but the lock
# remains held by this keeper until it explicitly unlocks or exits.
exec {lock_fd}>"$lock_path"

idle_checks=0
while :; do
  flock -x "$lock_fd"
  workloads=0
  # The scan uses only shell builtins while the shared lifecycle lock is held.
  # There are no helper processes to ignore here: PID 1 is Podman's init and
  # PID 2 is this keeper. Any other PID is a user/agent workload, including a
  # daemon re-parented to PID 1, except rootless Podman's pause process: it
  # only holds the nested user namespace and never exits by itself. Its exact
  # argv identifies it; pause.pid's location depends on the environment of
  # whichever exec first ran podman. An unreadable argv counts as work.
  for proc in /proc/[0-9]*; do
    pid=${proc##*/}
    (( pid <= 2 )) && continue
    [[ -d "$proc" ]] || continue
    argv=()
    mapfile -d '' -t argv <"$proc/cmdline" 2>/dev/null
    [[ ${#argv[@]} -eq 2 && ${argv[0]##*/} == catatonit && ${argv[1]} == -P ]] && continue
    workloads=1
    break
  done

  if (( workloads )); then
    idle_checks=0
  else
    ((idle_checks += 1))
    # Exit while still holding the same lock used by host-side exec admission.
    # A newly admitted exec therefore either acquired the lock first and is
    # observed above, or waits until this old box has disappeared.
    (( idle_checks >= 3 )) && exit 0
  fi
  flock -u "$lock_fd"
  sleep 1
done
