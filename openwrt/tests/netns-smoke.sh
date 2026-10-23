#!/bin/sh
# Run only in a disposable Linux/OpenWrt test container, never on a home router.
set -eu
[ "${PORTAL_TEST_DISPOSABLE:-}" = 1 ] || {
	echo 'Use a disposable container and set PORTAL_TEST_DISPOSABLE=1.' >&2
	exit 1
}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$HERE/netns_smoke.py"
