# LanInputShare

LanInputShare 是一个面向 Windows 10/11 的局域网键盘、鼠标和剪贴板共享工具。它允许一台可配置的主控电脑操作最多两台客户端电脑，每台电脑对应一个显示器，不依赖公网中转或第三方云服务。

> 当前项目主要面向个人局域网使用。Windows、网络与安全桌面的限制请先阅读[已知限制](docs/TROUBLESHOOTING.zh-CN.md#已知限制)。

## 主要功能

- 最多三台 Windows 电脑共享一套键盘和鼠标。
- 主控可以位于上、下、左、中、右任一位置，全部布局由主控统一设置。
- 鼠标跨越相邻屏幕边缘时按对应比例衔接到目标屏幕。
- 支持 `Ctrl+Alt+Esc` 紧急返回主控，以及 `Ctrl+Alt+1/2/3` 快速切换设备。
- 双向同步 Unicode 文本、Windows 图片/截图剪贴板。
- 支持文件和文件夹跨设备复制粘贴；传输前检测同名冲突，耗时超过约 1 秒时显示进度浮窗。
- 文件传输分块、限速并保留控制消息优先级，降低大文件占满 Wi-Fi 后造成的键鼠延迟。
- TLS 1.3 加密连接、设备身份校验和首次连接确认。
- 记忆设备名称、角色、网卡、客户端 IP、端口与屏幕布局。
- 支持托盘运行、暂停共享、返回主控、诊断导出和安全退出。

## 使用边界

| 项目 | 当前范围 |
|---|---|
| 操作系统 | Windows 10/11 64 位 |
| 设备数量 | 一台主控，最多两台客户端 |
| 显示器 | 每台电脑一个显示器 |
| 网络 | 同一局域网，主控填写客户端固定 IPv4 |
| 默认端口 | TCP `24870` |
| 代理兼容 | 可与 Clash/VPN/TUN 共存，但局域网地址必须走物理网卡 |
| 跨平台 | 暂不支持 macOS、Linux |
| 安全桌面 | 不支持登录界面和 UAC 安全桌面 |

## 快速开始

1. 在每台电脑上使用同一版本的完整发行目录。
2. 先启动客户端，设置不同的设备名称，选择“客户端”和实际使用的物理网卡，然后点击“保存并启动”。
3. 在主控上选择“主控”，设置本机位置，在十字布局中添加客户端的局域网 IPv4 和端口。
4. 点击“保存并启动”。首次连接时，到客户端电脑确认主控名称和证书短指纹。
5. 连接成功后，将鼠标从相邻边缘移出即可跨屏。

布局必须相邻。多台设备时需要占用“中”位置，例如：

```text
        上
左      中      右
        下
```

详细操作见[中文使用说明](docs/USAGE.zh-CN.md)。连接失败、VPN/TUN、托盘、剪贴板和 UAC 问题见[故障排查](docs/TROUBLESHOOTING.zh-CN.md)。

## 剪贴板与文件复制

- 文本和截图：在任意已连接设备复制后，会自动同步到其他已连接设备的剪贴板。
- 文件或文件夹：在来源设备复制，切换到目标设备后按 `Ctrl+V`，此时才开始传输。
- 如果目标位置已有同名项目，会在传输前选择“覆盖全部”“跳过冲突”或“取消传输”。
- 跨设备“剪切”按复制处理，不会删除来源文件。
- 小文件通常不显示进度；预计耗时超过约 1 秒时，目标屏幕顶部显示进度、速度和取消按钮。

## 从源码运行

要求 Python 3.13 64 位。

```powershell
python -m pip install -e ".[dev]"
python -m lan_input_share --diagnose
python -m lan_input_share
```

只显示托盘图标：

```powershell
python -m lan_input_share --minimized
```

## 构建 Windows 发行目录

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

构建脚本会创建隔离环境，安装锁定依赖，运行测试、Ruff 和 mypy，执行 PyInstaller，并生成：

- `dist\LanInputShare\LanInputShare.exe`
- `dist\LanInputShare.manifest.txt`
- `dist\LanInputShare.sha256`

## 配置、日志与卸载

运行数据位于 `%LOCALAPPDATA%\LanInputShare`：

- `config.json`：界面配置；
- `identity\`：本机身份密钥和证书，不要分享；
- `trusted-devices.json`：已信任设备；
- `logs\`：运行日志；
- `staging\`：跨设备文件传输暂存区。

卸载时先从托盘退出程序，再删除发行目录。若需要完全重置配对关系，可在确认不再需要原身份后删除 `%LOCALAPPDATA%\LanInputShare`；此操作会同时清除配置、身份、信任和暂存文件。

## 安全说明

- 建议仅在可信的家庭或办公局域网中使用。
- 防火墙只应对“专用网络”开放 TCP `24870`，不要直接暴露到公网。
- 首次连接应在客户端核对设备名称和短指纹。
- 诊断包会过滤常见敏感字段，但分享前仍应自行检查。
- 仓库当前没有附带开源许可证；除非仓库所有者另行授权，否则不要假定获得复制、修改或再分发许可。

## 开发验证

```powershell
python -m pytest -q
python -m ruff check .
python -m mypy src
```

真实设备验收可使用[Windows 设备验收模板](docs/acceptance/windows-device-checklist.md)。版本变化见 [CHANGELOG.md](CHANGELOG.md)。
