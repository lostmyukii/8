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

## 不能替代什么

这是动作级、确定性仿真，不是电机、轮胎打滑、编码器噪声和 VL53LXX 的高保真物理模型。真实车的 PID、方向、阈值和急停仍必须按硬件验证顺序验收。

## 本地无 Webots 协议测试

```bash
python3 -m simulation.webots.maze_car.standalone_server
python3 rdk_maze_tuner/main.py --tcp 127.0.0.1:8765 --mode action
```

## Webots 启动

```bash
webots simulation/webots/maze_car/worlds/maze_world.wbt
```

控制器只在 `127.0.0.1:8765` 监听，Dashboard 和 Webots Web Streaming 均应通过服务器本机或 SSH 隧道访问。
