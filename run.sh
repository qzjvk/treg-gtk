#!/bin/sh
# Run treg console straight from this checkout (no install needed).
cd "$(dirname "$0")"
exec python3 -m treg_console "$@"