# shellcheck shell=bash
# scripts/lib/cron-check.sh - the `--check` mode shared by every ensure-*.sh
# script that installs or edits a gateway cron row (issue 678).
#
# Sourced, never executed. A script calls `cron_check_args "$@"` first, guards
# every write (cron add/edit/enable/rm, file installs) with
# `[ "$CRON_CHECK" = 0 ]`, calls `cron_check_absent <name>` where it would add
# a missing row, `cron_check_agrees <name> <id>` after its fail-closed verify,
# and `cron_check_fail <reason>` where it cannot look at the live row at all. The
# verify itself is the same code in both modes, so `--check` compares the live
# row's agent, schedule (expr and tz), enabled flag, payload, trigger and
# delivery with what a converge sets; timeout, light-context and failure-alert
# settings are not all compared.
#
# Exit codes in --check mode: 0 the live row agrees with the declaration; 1
# drift (the row is missing, or the verify names a field that differs); 2 bad
# usage; 3 the check could not run (no gateway container, no owner chat id,
# unreadable cron list). scripts/check-cron-declarations.sh runs every --check,
# labels 1 drift and any other non-zero error, and the deploy fails on either.
# Converge mode keeps exit 1 for all of these.

CRON_CHECK=0

cron_check_args() { # "$@" of the calling script
  case "${1:-}" in
    --check) CRON_CHECK=1 ;;
    "") ;;
    *) echo "usage: $(basename "$0") [--check]" >&2; exit 2 ;;
  esac
}

cron_check_absent() { # $1 = job name; a missing row is drift in --check mode
  [ "$CRON_CHECK" = 1 ] || return 0
  echo "drift: cron '$1' is declared by $(basename "$0") but not live" >&2
  exit 1
}

cron_check_agrees() { # $1 = job name, $2 = job id; ends a --check run that passed
  [ "$CRON_CHECK" = 1 ] || return 0
  echo "ok: cron '$1' ($2) agrees with $(basename "$0")"
  exit 0
}

cron_check_fail() { # $1 = reason; the live row could not be checked
  echo "$1" >&2
  if [ "$CRON_CHECK" = 1 ]; then exit 3; fi
  exit 1
}

cron_check_declares() { # $1 = a declaring script; prints each cron row name it declares
  # The names a script passes to cron_check_absent/cron_check_agrees: its
  # top-level JOB_NAME= (and READER_NAME= for a second row) assignments. Read,
  # never run, so listing needs no gateway. A policy script (no such line)
  # prints nothing.
  sed -nE 's/^(JOB_NAME|READER_NAME)="?([A-Za-z0-9_.-]+)"?[[:space:]]*$/\2/p' "$1"
}
