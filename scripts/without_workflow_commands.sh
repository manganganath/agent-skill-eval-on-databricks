#!/usr/bin/env bash
# Runs a command with GitHub Actions workflow commands turned off for everything it prints, and exits with its status.
# For commands that print text from a pull request, such as skill file names: a name that holds a newline and
# `::notice ...` is then printed, not obeyed. Each run uses a fresh random token, and standard error joins standard
# output, so none of the command's output can arrive after the commands are turned back on.
set -uo pipefail

token=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
if [ "${#token}" -ne 32 ]; then
  echo "::error::Could not make a token to stop workflow commands."
  exit 1
fi
echo "::stop-commands::$token"
status=0
"$@" 2>&1 || status=$?
echo "::$token::"
exit "$status"
