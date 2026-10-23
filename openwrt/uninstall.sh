#!/bin/sh
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run as root.' >&2; exit 1; }
if command -v opkg >/dev/null 2>&1 && opkg status portal-bitrate 2>/dev/null | grep -q 'Status:.* installed'; then
	exec opkg remove portal-bitrate
fi
if [ -x /etc/init.d/portal-bitrate ]; then
	/etc/init.d/portal-bitrate stop
	/etc/init.d/portal-bitrate disable
fi
if [ -x /usr/sbin/portal-bitrate ]; then
	# The supervisor's exclusive lock prevents deletion during a running recovery.
	/usr/sbin/portal-bitrate repair
fi
[ ! -e /var/run/netns/portal-bitrate ] || { echo 'Recovery namespace remains. Files retained.' >&2; exit 1; }
rm -f /usr/sbin/portal-bitrate /etc/init.d/portal-bitrate
rm -rf /usr/lib/portal-bitrate
echo 'Removed. Configuration/enrollment retained in /etc/config/portal_bitrate and /etc/portal-bitrate.'
echo 'Distribution dependencies retained for other packages.'
