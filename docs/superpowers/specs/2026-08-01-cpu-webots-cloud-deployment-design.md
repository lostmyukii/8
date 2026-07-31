# RDK X3 + ESP32 迷宫小车 CPU 云端仿真部署设计

日期：2026-08-01  
状态：待用户书面确认后实施

## 1. 目标

在一台 Ubuntu 24.04 x86_64 CPU 云服务器上部署 ROS2、Webots、项目 Python 环境、现有 FastAPI Dashboard 和浏览器仿真入口，使两名参与者可以轮流使用同一套仿真环境。

项目仍保持现有硬件职责：

- RDK X3 负责建图、规划、视觉、调参和可视化。
- ESP32 负责电机、编码器、测距、动作闭环和本地安全。
- Mac 负责 Codex、VS Code、PlatformIO 编译及通过 USB 烧录 ESP32。
- 云服务器不直接控制真实电机，也不承担真实 ESP32 的 USB 烧录。

## 2. 已确认约束

- 服务器为 8 vCPU、16 GiB 内存、200 GiB SSD 的无 GPU 实例。
- 操作系统为 Ubuntu Server 24.04 LTS 64 位。
- 公网采用独立 IPv4、BGP、按流量计费和 10 Mbps 上限。
- 两名参与者使用同一个项目、同一个仿真环境，同一时间只由一人修改和控制。
- 不训练大模型；第一阶段使用 PID、规则调参、DFS/BFS/A* 和轻量视觉方案。
- 第一阶段模拟迷宫、车体、编码器和前/左/右距离传感器；不把高分辨率相机渲染作为验收条件。

## 3. 关键技术决定

### 3.1 CPU 服务器不承诺完整 Webots 编辑器流畅渲染

无 GPU 服务器不满足 Webots 推荐的硬件图形加速条件。因此部署采用双模式：

1. 日常模式：Webots 在 Xvfb 中运行，物理仿真由服务器计算，通过 Webots 自带的 Web Streaming 将三维场景传给 Mac 浏览器。
2. 自动测试模式：Webots 使用 `--no-rendering --batch` 运行，用于回归测试和参数实验。
3. 调试桌面模式：提供一套共享 Xfce/noVNC 桌面。Webots 完整窗口可以尝试启动，但帧率不是验收指标；该桌面主要用于终端、文件和低频场景检查。

如果 Webots 的浏览器流在纯 CPU 软件 OpenGL 下仍不能稳定工作，系统降级为“无界面物理仿真 + 现有 Dashboard 地图/遥测显示”，并记录真实错误，不把降级结果描述为完整三维仿真成功。

### 3.2 软件版本

- Ubuntu Server 24.04 LTS x86_64
- ROS2 Jazzy
- Webots R2025a 稳定版
- Python 3.12 系统运行时，项目使用独立虚拟环境
- FastAPI + Uvicorn + WebSocket
- Xvfb
- Xfce + TigerVNC + noVNC，仅作为共享调试桌面
- Nginx 不作为第一阶段前置依赖；第一阶段通过 SSH 隧道访问内部服务

不安装 OpenClaw、Hermes、DeepSeek 或其他应用市场预装智能体镜像。

## 4. 部署架构

```text
Mac
├── Codex / VS Code
├── 项目源文件（当前目录为源）
├── PlatformIO 编译和 USB 烧录
└── SSH 隧道
    ├── 本地 8000 -> 服务器 Dashboard 8000
    ├── 本地 6080 -> 服务器 noVNC 6080
    ├── 本地 1234 -> 服务器 Webots Stream 1234
    └── 本地 8001 -> 服务器 Streaming Viewer 8001

CPU 云服务器
├── ROS2 Jazzy
├── Webots R2025a
├── Xvfb / 软件 OpenGL
├── Webots Streaming Viewer
├── 项目 Python 虚拟环境
├── FastAPI Dashboard
├── 版本化发布目录
└── systemd 用户服务

RDK X3
├── Python 串口客户端
├── 迷宫建图与规划
└── 真实小车运行

ESP32
├── PID / 编码器 / VL53LXX
├── 非阻塞动作状态机
└── 心跳、急停和动作超时保护
```

## 5. 代码和数据流

### 5.1 代码

当前 Mac 目录是源文件的唯一真源。部署脚本把一个经过测试的文件快照上传到服务器的版本目录：

```text
/srv/maze/releases/<时间戳-内容摘要>/
/srv/maze/current -> releases/<当前版本>/
/srv/maze/shared/logs/
/srv/maze/shared/exports/
```

不使用 `rsync --delete`，不覆盖旧版本。切换 `current` 软链接即可回滚。

两名参与者轮流操作。开始修改前写入操作者锁，结束后释放；已有锁时第二个人只查看，不直接编辑。

### 5.2 仿真

1. Dashboard 或仿真启动脚本选择迷宫世界和参数快照。
2. Webots 启动差速小车、编码器和三路距离传感器模型。
3. 仿真适配器实现与真实 ESP32 相同的一行一个 JSON 协议。
4. 现有 `SerialClient`、`MazeRunner`、`MazeMap`、`MazePlanner` 和 `AutoTuner` 通过 fake/sim transport 工作。
5. 每个动作仍使用 `action_id`，并回传对应 `done` 或 `error`。
6. 仿真日志、参数变化和地图输出写入版本化实验目录。

真实串口与仿真 transport 不同时启用，避免仿真命令误发到实车。

### 5.3 实车

1. 仿真通过后，Mac 本地执行 PlatformIO 编译。
2. 只有 ESP32 通过 USB 连接 Mac 或 RDK X3 时才允许烧录。
3. 烧录前再次确认串口设备。
4. RDK X3 运行真实 Python 控制层。
5. 实车验收与仿真验收分别记录，不用仿真结果替代硬件证明。

## 6. 远程访问与安全

### 6.1 首次登录

- 用户提供的临时密码只用于首次 SSH 登录。
- 首次登录后安装一把项目专用 Ed25519 公钥。
- 先验证密钥登录成功，再关闭 SSH 密码登录。
- 原临时密码不写入文件、脚本、日志或命令参数。
- 用户在密钥登录验证后更换已经在聊天中出现过的密码。

### 6.2 网络

- 第一阶段公网仅需要 TCP 22。
- Dashboard、noVNC 和 Webots Stream 仅监听 `127.0.0.1`，通过 SSH 隧道访问。
- 不向公网直接开放 5900、6080、8000、8001、1234。
- TCP 443 保留给后续域名和 HTTPS 阶段，但第一阶段不依赖它。
- 安全组和 UFW 都拒绝其他入站端口。
- SSH 稳定后，将安全组的 22 端口来源限制为参与者公网地址；若地址经常变化，再单独评估 Tailscale。

### 6.3 服务账户

- `ubuntu` 仅用于系统维护和部署。
- 新建无 sudo 权限的 `maze` 账户运行 Webots、Dashboard 和仿真服务。
- 项目服务不以 root 身份运行。
- 两名参与者后续分别使用自己的 SSH 公钥，不共享私钥。

## 7. 服务管理

部署以下服务：

- `maze-dashboard.service`
- `maze-webots-stream.service`
- `maze-webots-headless@.service`
- `maze-novnc.service`

要求：

- 崩溃后有限次数自动重启。
- 所有服务日志进入 journald，并设置容量限制。
- Dashboard 关闭不影响 Webots 停止能力。
- 仿真超时后自动停止，不允许无限占用 CPU。
- 服务器重启后只自动启动 Dashboard；Webots 仿真由操作者显式启动。

## 8. 安装和验证顺序

### 阶段 A：安全和主机基线

1. 核对主机名、Ubuntu 版本、CPU、内存、磁盘和公网出口。
2. 安装并验证专用 SSH 密钥。
3. 创建 `maze` 账户和目录。
4. 配置 UFW、Fail2ban、时区和系统更新。
5. 记录安装前系统信息，不记录凭据。

### 阶段 B：项目 Python 环境

1. 上传项目版本快照。
2. 创建 Python 虚拟环境并安装 `requirements.txt`、`requirements-dev.txt`。
3. 运行：

```bash
python -m compileall rdk_maze_tuner
python -m pytest rdk_maze_tuner/tests -q
```

### 阶段 C：ROS2

1. 安装 ROS2 Jazzy 基础版和开发工具。
2. 验证环境脚本。
3. 运行 talker/listener 基础通信测试。

### 阶段 D：Webots

1. 安装 Webots R2025a、Xvfb 和软件 OpenGL 依赖。
2. 运行 `webots --sysinfo`。
3. 运行官方最小世界的无界面测试。
4. 启动 Web Streaming，使用 Mac 浏览器验证三维场景。
5. 再启动项目迷宫世界；相机保持禁用或低分辨率。

### 阶段 E：共享桌面和服务

1. 安装 Xfce、TigerVNC 和 noVNC。
2. 所有 VNC/noVNC 端口仅绑定本机回环地址。
3. 从 Mac 建立 SSH 隧道并验证浏览器访问。
4. 注册 systemd 服务并验证重启行为。

### 阶段 F：项目闭环

1. Sim transport 发送 `ready` 和 telemetry。
2. Dashboard 下发带 `action_id` 的动作。
3. 仿真返回 ack 和匹配的 done/error。
4. 迷宫位置只在 done 后更新。
5. 急停、动作超时和参数限幅测试通过。
6. 导出地图、参数历史和 JSONL 日志。

## 9. 验收标准

部署完成必须同时满足：

- SSH 密钥登录成功，密码登录已关闭。
- 服务器对公网没有暴露内部仿真端口。
- Python 测试全部通过。
- ROS2 talker/listener 通过。
- Webots 官方最小世界可以无界面运行。
- Mac 浏览器能通过 SSH 隧道看到 Webots Web Stream，或明确记录纯 CPU 图形兼容失败并启用降级方案。
- Dashboard 能显示仿真 telemetry、迷宫地图和动作结果。
- `action_id` 的 ack/done/error 闭环通过。
- 旧服务器发布版本可以回滚。
- PlatformIO 在 Mac 本地编译通过。
- 未经新鲜设备确认，不执行 ESP32 烧录。

## 10. 非目标

- 不在 CPU 服务器上运行 Isaac Sim。
- 不承诺无 GPU 的 Webots 完整编辑器达到高帧率。
- 不训练大模型或强化学习策略。
- 不让云服务器直接控制真实小车电机。
- 不让自动调参修改安全阈值。
- 不在第一阶段公开暴露 Dashboard 或远程桌面。

## 11. 回滚

- 系统包安装失败时保留错误日志，优先修复；若基础环境污染严重，使用腾讯云重装 Ubuntu 24.04，不删除本地项目。
- 项目版本失败时把 `/srv/maze/current` 切回上一个版本。
- Webots Stream 不稳定时切换到无界面模式，保留现有 Dashboard。
- noVNC 不稳定时使用 SSH + VS Code Remote SSH，不影响仿真服务。
- 真实硬件始终保留为独立验收链，云端失败不改变 ESP32 安全逻辑。
