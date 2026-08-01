# Webots 迷宫小车仿真

该仿真用于验证 RDK 侧的协议、建图、路径规划、Dashboard 和规则型调参闭环。

它保持真实 ESP32 使用的换行 JSON 协议：

- `action_id`
- `ack`
- `telemetry`
- `done`
- `error`
- `stop`
- `estop`

## 能验证什么

- 小车逐格前进和原地转向的可视化姿态
- 前、左、右虚拟测距
- 障碍阻挡和 `OBSTACLE_TOO_CLOSE`
- 动作完成与编码器累计值
- Dashboard 急停、参数和动作链路

## 两种仿真后端

- `maze_world.wbt` 是原有动作级确定性仿真，用于快速验证协议和业务流程。
- `maze_physical_world.wbt` 是刚体物理仿真，运动只能由两个轮电机产生，
  不允许控制器逐帧写小车坐标。
- `maze_physical_calibration.wbt` 是无墙标定场，带 250 mm 直行标记和
  90° 转向标记。

物理仿真包含轮胎/地面接触、左右轮独立材质、编码器、前/左/右 ToF、
IMU、陀螺仪、加速度计和被动万向轮。它可以帮助复现摩擦和打滑，但真实
车的 PID、传感器方向、安全阈值和急停仍必须按硬件验证顺序验收。

## 物理配置资产

`config/physical_profiles/` 保存第一版不可变物理配置：

- `normal-v1`：左右轮正常摩擦。
- `low-v1`：左右轮低摩擦。
- `asymmetric-v1`：左轮低摩擦、右轮正常摩擦。
- `local-patch-v1`：正常地面中启用局部低摩擦区域。

每个配置都包含轮径、轮距、质量、重心、惯量、电机、编码器、ToF、
IMU、接触参数、8 ms 控制周期、50 ms telemetry 周期和固定随机种子。
`PhysicalProfileRepository` 对字段、数值范围、质量一致性和文件边界进行
严格校验，并从规范化完整快照计算 SHA-256。

`PhysicalMazeCar.proto` 的默认几何参数与 `normal-v1` 一致：65 mm
轮径、135 mm 轮距。四种配置仍由不可变 YAML 和 SHA-256 摘要管理；
world 只给出可加载的默认物理场景，后续控制器按所选配置进行严格校验。
现有 `maze_world.wbt` 仍是稳定的确定性动画入口。

## 本地无 Webots 协议测试

```bash
python3 -m simulation.webots.maze_car.standalone_server
python3 rdk_maze_tuner/main.py --tcp 127.0.0.1:8765 --mode action
```

## Webots 启动

```bash
webots simulation/webots/maze_car/worlds/maze_world.wbt
webots simulation/webots/maze_car/worlds/maze_physical_calibration.wbt
webots simulation/webots/maze_car/worlds/maze_physical_world.wbt
```

控制器只在 `127.0.0.1:8765` 监听，Dashboard 和 Webots Web Streaming 均应通过服务器本机或 SSH 隧道访问。

P1 静止稳定性初验使用：

```bash
MAZE_P1_STABILITY=1 webots --batch --mode=fast \
  simulation/webots/maze_car/worlds/maze_physical_calibration.wbt
```

控制器运行 10 秒后只读输出 `MAZE_P1_STABILITY` JSON。报告包括位置漂移、
姿态变化、最大垂直速度、最大倾角、倾覆和穿地判断；P1 不发送电机命令。

## 自动化 P1–P4 物理验收

服务器使用独立临时端口和临时结果目录执行，不占用生产协议端口
`127.0.0.1:8765`：

```bash
python3 -m simulation.webots.maze_car.tools.run_physical_acceptance \
  --webots /usr/local/bin/webots \
  --world simulation/webots/maze_car/worlds/maze_physical_calibration.wbt \
  --scenarios simulation/webots/maze_car/config/acceptance_scenarios.yaml \
  --output /srv/maze/shared/acceptance/physical
```

每次运行生成一个不可变运行目录，至少包含：

- `events.jsonl`：ready、reset、动作、telemetry、done/error 和进程事件。
- `report.json`：源码 commit、Webots 版本、profile/map 摘要、seed、
  P1/P2 指标、逐场景阈值判定、实时倍率、8 ms 控制周期和错误。
- 每个场景的 `webots.log` 与初始场景截图。

Webots 不存在时报告状态是 `unavailable` 且命令返回非零；ready 超时、
子进程退出、协议断开、场景阈值失败或报告字段不完整同样返回非零，
不会把缺失证据写成 PASS。runner 只回收自己启动并写入 PID 文件的
Webots 进程，不使用 `pkill` 或 `killall`。

服务器的 stream、desktop、headless 三种服务均显式加载
`maze_physical_world.wbt` 和 `normal-v1`。它们互斥运行，协议只监听
`127.0.0.1:8765`；stream 继续输出 W3D，headless 保持 world 中定义的
8 ms 物理步长而关闭渲染。
