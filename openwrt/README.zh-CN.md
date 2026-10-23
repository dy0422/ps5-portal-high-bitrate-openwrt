# PS5 Portal High Bitrate：OpenWrt 安装包（实验版）

面向官方 OpenWrt 的独立移植，优先供 x86/x86_64 软路由测试。不是 OpenWrt 或原作者发布的官方包。
保留上游码率修改逻辑，65/100/200 Mbps 是请求目标，实际画质、吞吐和延迟需要实机确认。

## 工作方式

```text
现有 br-lan ── pb-host（veth）── [独立网络命名空间 portal-bitrate]
                                relay0 + 一个空闲 LAN IPv4 地址
                                上游 Python 中继 + ARP 恢复 guardian
```

只将 Portal → PS5 的控制/出站数据引到中继，PS5 → Portal 视频保持直达。
路由器主系统的 IP 转发、WAN、NAT、现有网络和防火墙配置不被改写。
只在独立命名空间内关闭内核转发，避免内核与 Python 重复转发。
不依赖 Docker，不需要重刷固件，不会自动下载主分支源码。

第一版只支持普通 Linux LAN 网桥（通常为 `br-lan`），PS5、Portal、中继在同一 IPv4 子网和二层网络。
访客隔离、跨 VLAN/跨子网和公网远程连接不在本版支持范围。带 VLAN filtering 的网桥需另行适配端口 VLAN。
PS5 建议有线连接。保留原来的主路由/旁路由角色即可，但该设备必须能通过指定网桥直接访问两台设备。

## 安装

### 使用 opkg 的 OpenWrt：直接安装 IPK

上传 `portal-bitrate_0.1.0-1_all.ipk` 到路由器 `/tmp`，然后：

```sh
opkg update
opkg install /tmp/portal-bitrate_0.1.0-1_all.ipk
portal-bitrate doctor
```

也可以在 LuCI → 系统 → 软件包中上传 IPK。安装包不附带 LuCI 配置页面，配置通过下面的命令完成。
`all` 表示程序是 Python/脚本，无架构相关二进制；内核模块和依赖由路由器自己的软件源提供。
依赖为 `python3 scapy ip-full kmod-veth libpcap`。必须使用与当前固件内核匹配的软件源，不能强制安装其他固件的 kmod。
`ip-full` 通常与 `ip-tiny` 并存，由系统 alternatives 选择完整版。

### 使用 apk 的新版 OpenWrt，或希望通过脚本安装

上传 `portal-bitrate-openwrt-0.1.0-1.tar.gz` 到 `/tmp`：

```sh
cd /tmp
tar xzf portal-bitrate-openwrt-0.1.0-1.tar.gz
cd portal-bitrate-openwrt
sh install.sh
portal-bitrate doctor
```

脚本自动选择 `opkg` 或 `apk`，从当前固件软件源安装依赖并校验包内文件，然后复制程序。
脚本安装的程序不登记到 apk 数据库，使用同一目录里的 `uninstall.sh` 卸载。
没有可用的软件源、NET_NS 或 veth 支持时会失败，不会修改主系统转发/防火墙来绕过问题。
IPK 安装与脚本安装二选一；已有安装时会拒绝覆盖。升级前停止并卸载旧版，配置/配对信息保留，升级后重新做 baseline。

## 配置和第一次测试

先给 PS5、Portal 设置 DHCP 静态租约，再给中继预留一个**不在 DHCP 动态分配范围内、且未被其他设备使用**的 IPv4 地址。
程序会做 ARP 冲突探测，但离线设备无法回应，所以仍需核对 DHCP 配置。

以下是假设 LAN 为 `192.168.1.0/24` 的示例，**必须换成你自己的地址**：

- PS5：`192.168.1.100`
- Portal：`192.168.1.101`
- 中继独立地址：`192.168.1.250/24`，不能用路由器自己的地址
- 网桥：`br-lan`

PS5 和 Portal 都开机，Portal 暂时断开串流，然后：

```sh
portal-bitrate configure --bridge br-lan --address 192.168.1.250/24 \
  --ps5 192.168.1.100 --portal 192.168.1.101 --profile 65
```

这一步探测空闲地址、记录两台设备的 MAC，写入 UCI `/etc/config/portal_bitrate` 和私有配对文件。
命名空间用完即删；服务保持关闭。配置失败时不会启用中继。

运行未修改码率的基线测试：

```sh
portal-bitrate baseline
```

等 `BASELINE READY` 后连接 Portal，确认画面和按键。程序在约 60 秒后结束，恢复 ARP 并保存基线。
没有观察到转发数据、恢复失败或配置/代码不匹配时，不允许启动主动修改。

运行前台提码率试验：

```sh
portal-bitrate try
```

等 `AUTO READY` 后让 Portal 重新连接。查看 Portal 码率显示并实际玩游戏。
按 Ctrl+C 停止，等待程序完成 ARP 恢复，确认 Portal 恢复普通连接。
计数 `unique_modified_startups` 只能说明本地发出了修改后的报文，不能证明 PS5 接受或实际达到目标码率。

## 开机自动运行

只有在上述测试成功且画面/控制正常后，执行：

```sh
uci set portal_bitrate.main.enabled='1'
uci commit portal_bitrate
/etc/init.d/portal-bitrate enable
/etc/init.d/portal-bitrate start
portal-bitrate status
```

主路由转发保持不变。中继常驻并处理每次新连接，不必每次手动操作。
重启路由器后，再检查一次 Portal 新连接。

```sh
/etc/init.d/portal-bitrate running
portal-bitrate status
logread -e portal-bitrate
```

procd 不做故障自动重启，以免恢复尚未完成就重新引流。网络重载、网桥变更后，停止服务并重新测试。
如果启动时网桥尚未准备好，查看日志并手动启动。状态文件带时间戳，旧的 `running` 记录不能替代服务/进程检查。

## 停止、恢复和调整码率

```sh
uci set portal_bitrate.main.enabled='0'
uci commit portal_bitrate
/etc/init.d/portal-bitrate stop
/etc/init.d/portal-bitrate disable
portal-bitrate repair
```

停止会给恢复进程留出时间，完成后删除本工具自己的 veth 和命名空间。
`repair` 只在没有活动中继/guardian 时操作；不删除不属于本工具的网络设备、命名空间或锁文件。
恢复失败时保留恢复标记和网络路径，不继续启动下一次修改。
如果路由器断电或恢复命名空间已丢失，先让 Portal 断开重连以刷新 ARP，再重启路由器重新测试。
不要手动删除活动锁、网络命名空间或强杀所有 Python 进程。

改 100 Mbps：先按上面的步骤停止，然后重新执行 configure（把 `--profile 65` 换成 `100`）、baseline 和 try。
200 Mbps 仍是上游实验档，第一轮不建议用它。

## 卸载

IPK 安装：

```sh
opkg remove portal-bitrate
```

脚本安装：在解压目录执行 `sh uninstall.sh`。
卸载先停止并恢复 ARP，保留 `/etc/portal-bitrate` 配对/基线和 UCI 配置以供备份。
公共软件依赖不自动删除。配对文件包含局域网 IP/MAC，不要上传公开 issue。

## 已验证范围与待验证项

构建包不需要 OpenWrt SDK：`python3 openwrt/build.py`。
离线测试：`.venv/bin/python -m unittest discover -s tests -v` 和
`.venv/bin/python -m unittest discover -s openwrt/tests -v`。

已在官方 `openwrt/rootfs:x86-64-24.10.5` 隔离容器中验证：

- 官方软件源的 Python 3.11.16 / Scapy 2.5.0：37 项上游测试、15 项移植/打包测试通过。
- IPK 的 opkg 安装、procd 服务注册/启动/停止，以及卸载清理。
- 真实 veth/netns 和 UDP 通信：60 秒未修改基线、修改握手并送达模拟 PS5、返回数据直达模拟 Portal。
- SIGTERM 停止、SIGKILL 终止中继后的 guardian/ARP 恢复，以及路由器转发始终保持开启。

容器共用开发机内核，测试安装时跳过了 `kmod-veth` 依赖，使用宿主内核的 veth；没有验证你所用固件的内核模块安装。
**真实 OpenWrt 路由器、PS5/Portal、重启后的服务启动，以及 apk 安装路径仍未实机验证。**
上述结果不能证明固件兼容性、画质收益或实际码率。实际安装仍由固件自己的软件源解析全部依赖，不跳过依赖。
源码的 `openwrt/tests/netns-smoke.sh` 和 `procd_smoke.py` 可在一次性测试容器中复现网络与服务测试；
不要在家用路由器上运行这些测试脚本，它们会创建模拟网络并覆盖测试容器里的配对配置。
遇到 `Operation not permitted`、veth 模块缺失、网桥/防火墙丢包等错误时，保留日志并排查具体环境。
本版不自动禁用硬件加速或放开防火墙。

## 来源与许可

上游：https://github.com/atameric/ps5-portal-high-bitrate ，MIT，完整上游提交号记录在 `app/UPSTREAM_REV`。
OpenWrt 新增代码同样使用 MIT 许可，保留上游 LICENSE。依赖 Scapy 由 OpenWrt 软件源独立提供，许可见发行版包。
本项目与 Sony、PlayStation、OpenWrt 均无关联。
