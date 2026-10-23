#!/bin/sh
# Install the reviewed local bundle; never fetch or execute remote source code.
set -eu
umask 077
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP=/usr/lib/portal-bitrate

fail() { echo "portal-bitrate: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail 'Run as root on OpenWrt.'
[ -f /etc/openwrt_release ] || fail 'This installer is for OpenWrt.'
command -v uci >/dev/null 2>&1 || fail 'UCI is required.'
[ ! -e "$APP" ] || fail 'Already installed. Stop/uninstall the old version first; keep your config backup.'
[ ! -e /etc/init.d/portal-bitrate ] || fail 'A portal-bitrate service already exists.'
[ ! -e /usr/sbin/portal-bitrate ] || fail 'A portal-bitrate command already exists.'
[ ! -e /var/run/netns/portal-bitrate ] || fail 'An existing relay namespace needs recovery first.'
[ ! -e /var/run/portal-lab-capture.lock ] || fail 'Another relay or recovery lock exists.'

if [ "${1:-}" = '--remove' ]; then
	fail 'For removal use: sh uninstall.sh'
fi

echo 'Installing distribution packages: python3 scapy ip-full kmod-veth libpcap luci rpcd'
if command -v apk >/dev/null 2>&1; then
	apk update
	apk add python3 scapy ip-full kmod-veth libpcap luci rpcd
elif command -v opkg >/dev/null 2>&1; then
	opkg update
	opkg install python3 scapy ip-full kmod-veth libpcap luci rpcd
else
	fail 'No supported OpenWrt package manager (opkg/apk) found.'
fi

python3 - "$HERE" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
for name, expected in json.loads((root / 'MANIFEST.json').read_text()).items():
    if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
        raise SystemExit('Bundle checksum mismatch: ' + name)
from scapy.arch.common import compile_filter
from scapy.data import DLT_EN10MB
compile_filter('ether dst 02:00:00:00:00:01 and ip', linktype=DLT_EN10MB)
print('Bundle checksums and Scapy/libpcap: OK')
PY

mkdir -p "$APP" /etc/portal-bitrate
chmod 700 "$APP" /etc/portal-bitrate
cp "$HERE"/app/* "$APP/"
chmod 600 "$APP"/*
cp "$HERE/files/portal-bitrate" /usr/sbin/portal-bitrate
cp "$HERE/files/portal-bitrate.init" /etc/init.d/portal-bitrate
chmod 755 /usr/sbin/portal-bitrate /etc/init.d/portal-bitrate
(
	umask 022
	cp -R "$HERE/luci/root/." /
	cp -R "$HERE/luci/htdocs/." /www/
)
chmod 755 /usr/libexec/rpcd/portal-bitrate
if [ ! -e /etc/config/portal_bitrate ]; then
	cp "$HERE/files/portal_bitrate" /etc/config/portal_bitrate
fi
chmod 600 /etc/config/portal_bitrate
uci set portal_bitrate.main.enabled=0
uci commit portal_bitrate
/etc/init.d/portal-bitrate disable
rm -f /tmp/luci-indexcache*
if /etc/init.d/rpcd running >/dev/null 2>&1; then /etc/init.d/rpcd reload; fi
echo 'Installed, service disabled. Open LuCI: Services -> PS Portal bitrate.'
