#!/bin/sh
# Compatibility with the initial private-publication instructions.
set -eu
exec sh "$(dirname "$0")/push-reviewed.sh" "$@"
