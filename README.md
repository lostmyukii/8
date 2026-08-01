# RDK X3 + ESP32 迷宫小车

项目目标是完成自动建图、动作决策、ESP32 精准运动执行、实时参数调节、规则型自动调参和 Web 可视化。

## 分工边界

- RDK X3：感知、建图、规划、规则型调参和 Dashboard。
- ESP32：电机、编码器、前/左/右 VL53LXX、动作闭环和本地安全保护。
- Webots：在没有真实小车时验证动作协议、地图、路径规划和界面联调。

真实控制和仿真都使用一行一个 JSON 的协议，并保留 `action_id`、`ack`、`telemetry`、`done` 和 `error`。

## 本地测试

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m compileall rdk_maze_tuner simulation
.venv/bin/python -m pytest rdk_maze_tuner/tests -q
```

ESP32 固件：

```bash
cd esp32_firmware
pio run
```

## 不启动 Webots的协议闭环

终端一：

```bash
.venv/bin/python -m simulation.webots.maze_car.standalone_server
```

终端二：

```bash
.venv/bin/python rdk_maze_tuner/main.py \
  --tcp 127.0.0.1:8765 \
  --mode action \
  --action move_cell
```

## 云服务器入口

服务器部署文件位于 `deploy/server`。生产入口为
`https://8.ilelezhan.cn`，Caddy 只通过 80/443 代理 FastAPI；
`/simulation/*` 必须先通过网站会话鉴权。内部端口不直接暴露公网。

SSH 隧道保留为维护和公网故障时的回退：

- Dashboard：`http://127.0.0.1:8000/`
- Webots Web Streaming：`http://127.0.0.1:1234/index.html`
- 共享 Xfce/noVNC 桌面：`http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=remote`

使用 `deploy/mac/open_tunnels.sh` 建立隧道，使用 `deploy/mac/open_views.sh` 打开三个入口。

仿真模式：

```bash
sudo maze-sim-mode stream
sudo maze-sim-mode desktop
sudo maze-sim-mode headless
sudo maze-sim-mode status
```

服务器不承担 ESP32 的 USB 烧录。PlatformIO 编译、烧录和真实硬件验收仍在连接小车的 Mac 上完成。

物理仿真验收和服务操作见：

- `docs/acceptance/webots-physical-maze-car-checklist.md`
- `docs/operations/webots-physical-simulation-runbook.md`
