#!/bin/sh
set -eu
TASK_PID_FILE=/var/run/schemashield-docker.pid
test -f "$TASK_PID_FILE" || exit 0
read -r TASK_PID < "$TASK_PID_FILE" || test -n "$TASK_PID"
case "$TASK_PID" in ''|*[!0-9]*) echo 'Invalid owned daemon PID' >&2; exit 1;; esac
TASK_COMMAND=$(tr '\000' ' ' < "/proc/$TASK_PID/cmdline")
case "$TASK_COMMAND" in
  *--data-root=/var/lib/schemashield-docker*) kill -TERM "$TASK_PID";;
  *) echo 'Refusing to stop an unrelated process' >&2; exit 1;;
esac
