# OpenWrt v0.2.0 安装包

- [下载 IPK（opkg 系统，含 LuCI 设置界面）](portal-bitrate_0.2.0-1_all.ipk?raw=true)
- [下载脚本安装包（自动选择 opkg/apk）](portal-bitrate-openwrt-0.2.0-1.tar.gz?raw=true)
- [SHA256 校验值](SHA256SUMS)
- [中文安装与使用说明](../../openwrt/README.zh-CN.md)

安装后刷新或重新登录 LuCI，进入 **服务 → PS Portal 提码率**。
默认停用；先保存并检测设备，再完成连接测试和试运行。首轮从 65 Mbps 开始。

本版经过 70 项离线测试及官方 OpenWrt 24.10.5 容器中的 LuCI 浏览器、模拟设备通信、升级和恢复测试。
实际路由器、PS5/Portal、冷启动和 apk 安装路径仍需实机确认。
测试容器共用宿主内核，未验证目标固件的内核模块安装。请使用本机固件的软件源解析全部依赖。

安装包由仓库中的 `python3 openwrt/build.py` 生成，保留上游 MIT 许可。
