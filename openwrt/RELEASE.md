# OpenWrt v0.2.0 preview

Experimental x86/x86_64 OpenWrt packaging with a built-in LuCI settings page for the upstream PS5 Portal bitrate relay.

- Direct-install IPK: `portal-bitrate_0.2.0-1_all.ipk`.
- Portable installer for opkg/apk systems: `portal-bitrate-openwrt-0.2.0-1.tar.gz`.
- `SHA256SUMS` verifies the release asset contents.
- Chinese setup and recovery guide: [README.zh-CN.md](README.zh-CN.md).

The port uses an owned veth pair and network namespace on an existing LAN bridge,
leaving the router's forwarding and firewall configuration intact. It includes UCI
configuration, a procd service, web and foreground baseline/active trials, ARP recovery,
read-only diagnostics, and uninstall hooks. The service starts disabled. Open **Services → PS Portal 提码率** to configure devices, select a profile, run 60/90-second tests, start/stop, manage autostart, and inspect logs. Long operations run as tracked background jobs; interrupted baseline tests cannot create a valid receipt. The rpcd ACL exposes only the specific application methods.

Validation completed:

- 37 upstream tests and 33 supervisor/package/rpcd tests passed.
- Tested with Python 3.11.16 and distribution Scapy 2.5.0 in the official
  `openwrt/rootfs:x86-64-24.10.5` container, as well as development Scapy 2.6.1.
- Real veth/netns communication with simulated PS5/Portal peers: 60-second baseline,
  modified startup delivery, direct return traffic, graceful shutdown, and recovery
  after killing the relay engine. Router forwarding remained enabled.
- opkg package installation/removal, upgrade from 0.1.0 to 0.2.0, and procd start/stop passed in that container.
- Real Chromium/LuCI checks covered form validation/enrollment, a full 60-second baseline, cancellation without a success receipt, trial mutation/cancellation, service/autostart controls, diagnostics, anonymous access denial and mobile layout. Tests used simulated peers, not a real console.

The container shares its host kernel. Its install test skipped the firmware-specific
`kmod-veth` dependency and used the host's veth support. Normal installation resolves
all dependencies from the router's own feeds; do not bypass kernel dependencies.

Real router hardware, PS5/Portal compatibility, cold-boot startup, and the apk installer
path remain unverified. Requested bitrate is not measured throughput or PS5 acceptance.
Only a plain bridged IPv4 LAN is supported in this preview; VLAN-aware bridge ports
need additional adaptation. Start with the 65 Mbps profile.

Based on upstream commit `4430daaa0ea9f2baa498fee45540f6f8f4e19617`.
MIT licensed; upstream attribution and license are preserved.
