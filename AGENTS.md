# AGENTS.md

## 项目身份

这是一个 RDK X3 + ESP32 迷宫小车项目。目标是实现自动建图、动作决策、ESP32 精准运动执行、实时参数调节、规则型自动调参和 Web 可视化调参。

任何 agent 在本目录工作前，必须先阅读：

1. `RDK_X3_ESP32_小车迷宫全自动实时电子调参系统设计方案.md`
2. `DEVELOPMENT.md`
3. 当前要修改的源文件

当前目录不是 Git 仓库。不要假设可以通过提交记录恢复现场。

## 架构铁律

1. RDK X3 负责“看、想、建图、决策、调参、可视化”。
2. ESP32 负责“电机、编码器、测距、动作闭环、安全保护”。
3. RDK X3 不做高频 PWM 控制，不以 10ms 级频率发送左右轮 PWM。
4. ESP32 必须能在 RDK X3 断联、Python 卡死、Web 页面关闭时独立停车。
5. 自动调参只能修改运动和判断参数，不能静默修改安全参数。
6. 所有动作命令必须有 `action_id`，动作完成必须回传对应 `done` 或 `error`。
7. 串口协议使用一行一个 JSON，以 `\n` 结尾。
8. 第一阶段视觉只用于终点/标志识别和录像，不参与电机实时闭环。

## 当前硬件和引脚事实

根据现有 `main.cpp`：

- 左电机控制：GPIO 2、GPIO 4
- 右电机控制：GPIO 13、GPIO 27
- 左编码器：GPIO 25、GPIO 26
- 右编码器：GPIO 16、GPIO 17
- VL Front XSHUT：GPIO 18
- VL Back XSHUT：GPIO 23
- VL Right XSHUT：GPIO 19
- VL Left XSHUT：GPIO 5

用户描述的目标传感器为 3 个 VL53LXX-V2：前、左、右。现有代码中的 Back 传感器视为历史/可选功能，除非用户明确要求，不要让第一阶段核心逻辑依赖后向测距。

## 现有文件状态

- `main.cpp`：当前 ESP32 程序，使用 micro-ROS、阻塞式动作函数、固定参数。
- `ros2-example-new.py`：当前 RDK/PC 侧 ROS2 程序，包含 DFS 建图原型。
- `RDK_X3_ESP32_小车迷宫全自动实时电子调参系统设计方案.md`：产品和系统方案来源。
- `小车形态1.jpg`、`小车形态2.jpg`、`小车形态3.jpg`：硬件结构参考图。
- `DEVELOPMENT.md`：本轮形成的工程开发蓝图。

## 推荐技术路线

ESP32：

- PlatformIO + Arduino framework。
- 逐步从当前 `main.cpp` 拆分为 `protocol`、`params`、`motor`、`encoder`、`tof_sensors`、`motion_controller`、`safety`。
- 动作控制必须改为非阻塞状态机。
- 运动控制 tick 建议 5ms-10ms。
- telemetry 建议 10Hz-20Hz。

RDK X3：

- Python 直接串口通信，优先使用 `pyserial`。
- Web 调参服务优先使用 FastAPI + WebSocket。
- 参数文件使用 YAML。
- 实验日志使用 JSONL。
- 迷宫地图和自动调参逻辑必须可以在无硬件 fake client 下测试。

## 串口协议要求

RDK X3 发给 ESP32 的核心消息：

```json
{"type":"heartbeat","seq":1,"ts_ms":123456}
{"type":"set_params","seq":2,"params":{"base_speed":0.25,"turn_speed":0.18,"cell_ticks":1350}}
{"type":"action","seq":3,"action_id":"a-0001","name":"move_cell","speed":0.25,"target_ticks":1350}
{"type":"stop","seq":4}
{"type":"estop","seq":5,"reason":"dashboard"}
```

ESP32 回给 RDK X3 的核心消息：

```json
{"type":"ready","fw":"maze-esp32","version":"0.1.0"}
{"type":"ack","seq":3,"ok":true}
{"type":"telemetry","state":"IDLE","front_mm":300,"left_mm":180,"right_mm":260,"enc_left":0,"enc_right":0}
{"type":"done","action_id":"a-0001","name":"move_cell","success":true,"duration_ms":2200,"enc_left":1352,"enc_right":1347}
{"type":"error","action_id":"a-0001","code":"OBSTACLE_TOO_CLOSE","front_mm":55}
```

不要回到只用单个 `Int32` 表示动作的接口，除非是在保留旧程序做临时验证。

## 安全要求

ESP32 本地必须实现：

- 心跳超时停车。
- 前方过近停车。
- 动作超时停车。
- 急停优先。
- 参数限幅。
- 串口 JSON 解析失败不影响电机停止能力。

Web 页面必须始终显示急停入口。自动调参建议必须可追溯，至少记录参数旧值、新值、原因和触发动作。

## 编码规范

通用：

- 保持中文文档清晰，代码标识符用英文。
- 不要把硬件安全阈值散落在多个文件中。
- 不要在没有验证的情况下删除现有 pin map。
- 不要引入复杂框架来替代简单可测的串口、状态机和规则引擎。

ESP32：

- 避免长时间 `while` 阻塞动作执行。
- 所有动作通过状态机推进。
- 中断服务函数只做轻量计数，不做串口输出和复杂计算。
- PWM、PID、编码器、测距、协议解析分层处理。
- 所有参数都有默认值和范围。

RDK X3：

- 串口读取、Dashboard、迷宫决策不能互相阻塞。
- 发送动作后必须等待对应 `action_id` 的 `done` 或 `error`。
- `MazeMap` 和 `MazePlanner` 不直接访问串口。
- `AutoTuner` 不直接写 ESP32，只生成参数变更，由 `ParamManager` 校验后下发。

## 验证命令

RDK X3 Python：

```bash
python3 -m compileall rdk_maze_tuner
python3 -m pytest rdk_maze_tuner/tests -q
```

ESP32 PlatformIO：

```bash
pio run
pio run -t upload
pio device monitor -b 115200
```

Dashboard：

```bash
python3 rdk_maze_tuner/dashboard/app.py --host 0.0.0.0 --port 8000
```

如果当前阶段还没有对应目录或测试，agent 应说明“尚未创建”，不要假装验证通过。

## 硬件验证顺序

1. 串口连接：确认 RDK X3 能看到 ESP32，收到 `ready`。
2. 测距验证：用手遮挡前、左、右，确认数值方向正确。
3. 编码器验证：抬起小车转动左右轮，确认计数方向正确。
4. 电机方向：低 PWM 点动，确认左右轮方向。
5. 前进短距离：先 10cm，再一格。
6. 原地转向：先低速 45 度，再 90 度。
7. 急停：动作中触发 `estop`，必须立即停车。
8. 简单迷宫：2x2 或 3x3 验证建图。

## 文件修改原则

- 修改代码前先说明要改什么。
- 大改前先保留旧程序语义，避免一次性同时改协议、PID、建图和 UI。
- 优先实现可验证的最小闭环：串口握手、telemetry、动作、done/error。
- 新增依赖要写入文档或依赖文件。
- 不要把真实设备路径、Wi-Fi 密码、远程地址或其他敏感信息写入日志或文档。

