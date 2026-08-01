# RDK X3 + ESP32 迷宫小车全自动实时调参系统开发文档

## 0. 当前开发结论

本项目第一阶段不再把 RDK X3 当作毫秒级电机控制器。RDK X3 负责地图、策略、参数管理、自动调参和可视化；ESP32 负责电机、编码器、测距、安全保护和动作闭环执行。

当前目录中的 `main.cpp` 与 `ros2-example-new.py` 是可用的原型参考，但它们还不是最终目标架构：

- `main.cpp` 使用 micro-ROS，发布 `UInt16MultiArray` 测距数据，订阅 `Int32` 动作命令。
- `ros2-example-new.py` 使用 ROS2 节点做 DFS 建图和动作发布。
- 用户目标是 RDK X3 在 Linux 上直接运行 Python，通过 RDK X3 的 USB 口和 ESP32 串口通信。
- 因此第一阶段建议迁移到“换行 JSON 串口协议”，同时保留现有 ROS2 文件作为算法参考。

第一阶段验收目标：

1. ESP32 可以稳定读取左右编码器和前、左、右 3 个 VL53LXX/VL53L0X 测距值。
2. ESP32 可以执行 `move_cell`、`turn_left`、`turn_right`、`turn_back`、`stop`、`estop`。
3. RDK X3 可以通过串口下发动作、接收 telemetry 和 done/error 回执。
4. RDK X3 可以构建二维迷宫地图，使用 DFS 探索。
5. RDK X3 可以在 Web 页面中实时展示状态、地图、日志和可调参数。
6. 自动调参第一版使用规则引擎，不依赖大模型，不修改安全参数。

## 1. 硬件结构与角色

从现场照片和用户描述确认，小车采用上下两层结构：

| 层级 | 硬件 | 角色 |
| --- | --- | --- |
| 上层 | RDK X3 | Linux + Python，上位机决策、建图、视觉、调参、Web 服务 |
| 上层 | USB 转串口输入设备 | RDK X3 与 ESP32 的串口通信通道 |
| 上层前部 | 摄像头 | 第一阶段只用于终点/标志识别和录像，不进入电机主闭环 |
| 下层 | ESP32 | PlatformIO/Arduino，底层运动闭环、传感器读取、安全保护 |
| 下层 | 两个 TT 霍尔编码器电机 | 差速驱动，提供左右轮码盘反馈 |
| 下层 | 3 个 VL53LXX-V2 激光测距 | 前、左、右墙体检测 |

现有 `main.cpp` 中定义了 4 个 VL53L0X 对象：`VLFront`、`VLBack`、`VLRight`、`VLLeft`。用户当前目标为 3 个传感器，因此后续实现应把后向传感器作为可选项处理，默认协议只依赖 `front_mm`、`left_mm`、`right_mm`。

## 2. 当前代码审计

### 2.1 ESP32 侧 `main.cpp`

当前可复用内容：

- 电机引脚已有映射：
  - 左电机：GPIO 2、GPIO 4
  - 右电机：GPIO 13、GPIO 27
- 编码器已有中断读取：
  - 左编码器：GPIO 25、GPIO 26
  - 右编码器：GPIO 16、GPIO 17
- VL53L0X 地址初始化已有雏形：
  - Front XSHUT: GPIO 18
  - Back XSHUT: GPIO 23
  - Right XSHUT: GPIO 19
  - Left XSHUT: GPIO 5
- 已有前进、后退、左转、右转、停止函数。

当前主要问题：

- 动作函数使用 `while` 阻塞循环，动作执行时无法及时处理急停、心跳超时和新参数。
- 参数写死在全局变量和 `const` 中，例如 `leftPWM`、`rightPWM`、`thrhold`、`lhold`。
- 目前是简单比例修正，不是完整 PID，也没有参数限幅和动作结果报告。
- micro-ROS timer 设置为 `10000 ms`，不能满足 10Hz-20Hz telemetry 需求。
- 只接收一个 `Int32` 命令，无法携带动作 id、速度、目标 ticks、参数更新和安全状态。
- 动作完成后没有结构化 `done` 回执，RDK 侧无法可靠分析误差。

### 2.2 RDK X3 侧 `ros2-example-new.py`

当前可复用内容：

- `MazeExplorer` 已有坐标、朝向、墙体记录、DFS 探索和 ASCII 地图渲染。
- 已有前/后/右/左测距转全局方向的思路。
- 已有动作编码：`1=前进`、`2=后退`、`3=右转`、`4=左转`。

当前主要问题：

- 依赖 ROS2 与 micro-ROS，不符合“RDK X3 直接 Python 串口”的目标。
- 发送命令后立即在软件中更新位置，没有等待 ESP32 的动作完成回执。
- 只基于传感器回调节流发送动作，没有动作状态机。
- 没有参数管理、动作日志、自动调参、Web 调参界面。

## 3. 目标软件架构

```text
RDK X3 / Linux / Python
├── SerialClient：串口 JSON 协议、重连、心跳、动作请求/回执
├── ParamManager：params.yaml + limits.yaml，负责参数校验和版本号
├── MazeMap：格子地图、墙体、访问状态、坐标和朝向
├── MazePlanner：DFS 探索，后续扩展 BFS/A*
├── MotionAnalyzer：根据 done/telemetry 分析走偏、走长、转向误差
├── AutoTuner：规则型自动调参，受 limits.yaml 约束
├── Dashboard：Web 可视化调参、地图、日志、急停
└── Logger：JSONL 实验日志、参数变化记录、地图导出

USB 串口 / newline JSON

ESP32 / PlatformIO / Arduino
├── Protocol：解析 RDK 命令，发送 ready/ack/telemetry/done/error
├── MotionController：非阻塞状态机，执行一格前进和转弯
├── MotorDriver：PWM、方向、刹车
├── Encoder：左右轮计数和速度估算
├── PID：左右轮速度闭环和直行同步
├── ToFSensors：前、左、右测距，滤波和异常值处理
├── Params：运行时参数表和限幅
└── Safety：心跳超时、前方过近、动作超时、急停
```

关键原则：

1. RDK X3 只发送动作级命令，不连续发送左右 PWM。
2. ESP32 每 5ms-10ms 执行运动控制 tick，不能被串口读取或测距读取长期阻塞。
3. 每个动作都带 `action_id`，ESP32 回传同一个 `action_id` 的 `done` 或 `error`。
4. 参数可以运行时修改，但必须经过 `limits.yaml` 和 ESP32 本地限幅双重保护。
5. Web 页面上的手动调参、自动调参和 ESP32 实际生效参数必须能对齐显示。

## 4. 建议项目结构

第一阶段可以在当前目录内逐步演进为下面结构：

```text
.
├── AGENTS.md
├── DEVELOPMENT.md
├── RDK_X3_ESP32_小车迷宫全自动实时电子调参系统设计方案.md
├── legacy/
│   ├── main.cpp
│   └── ros2-example-new.py
├── esp32_firmware/
│   ├── platformio.ini
│   ├── include/
│   │   ├── config.h
│   │   ├── protocol.h
│   │   ├── params.h
│   │   ├── motor.h
│   │   ├── encoder.h
│   │   ├── tof_sensors.h
│   │   ├── motion_controller.h
│   │   └── safety.h
│   └── src/
│       ├── main.cpp
│       ├── protocol.cpp
│       ├── params.cpp
│       ├── motor.cpp
│       ├── encoder.cpp
│       ├── tof_sensors.cpp
│       ├── motion_controller.cpp
│       └── safety.cpp
├── rdk_maze_tuner/
│   ├── main.py
│   ├── config/
│   │   ├── params.yaml
│   │   └── limits.yaml
│   ├── core/
│   │   ├── serial_client.py
│   │   ├── param_manager.py
│   │   ├── maze_map.py
│   │   ├── maze_planner.py
│   │   ├── motion_analyzer.py
│   │   ├── auto_tuner.py
│   │   ├── safety_guard.py
│   │   └── logger.py
│   ├── dashboard/
│   │   ├── app.py
│   │   ├── static/
│   │   └── templates/
│   └── tests/
└── logs/
```

如果暂时不搬动文件，第一轮代码也可以直接在顶层 `main.cpp` 与 `ros2-example-new.py` 上改造。但长期维护建议使用上面的分层结构。

## 5. 串口 JSON 协议

协议使用 UTF-8 JSON，每条消息以 `\n` 结尾。波特率调试阶段用 `115200`，稳定后可切到 `460800` 或 `921600`。

### 5.1 RDK X3 发给 ESP32

心跳：

```json
{"type":"heartbeat","seq":1001,"ts_ms":123456}
```

批量设置参数：

```json
{"type":"set_params","seq":1002,"params":{"base_speed":0.25,"turn_speed":0.18,"cell_ticks":1350,"turn_90_ticks":720,"speed_kp":0.8,"speed_kd":0.05,"wall_threshold_mm":150,"front_stop_mm":120}}
```

前进一格：

```json
{"type":"action","seq":1003,"action_id":"a-0001","name":"move_cell","speed":0.25,"target_ticks":1350}
```

右转 90 度：

```json
{"type":"action","seq":1004,"action_id":"a-0002","name":"turn_right","speed":0.18,"target_ticks":720}
```

左转 90 度：

```json
{"type":"action","seq":1005,"action_id":"a-0003","name":"turn_left","speed":0.18,"target_ticks":720}
```

停止：

```json
{"type":"stop","seq":1006}
```

急停：

```json
{"type":"estop","seq":1007,"reason":"dashboard"}
```

### 5.2 ESP32 回给 RDK X3

启动完成：

```json
{"type":"ready","fw":"maze-esp32","version":"0.1.0","imu_available":false,"features":["motor","encoder","tof","imu_optional","json_serial"]}
```

命令确认：

```json
{"type":"ack","seq":1003,"ok":true}
```

实时状态：

```json
{"type":"telemetry","uptime_ms":123456,"state":"MOVING_CELL","front_mm":320,"left_mm":180,"right_mm":260,"enc_left":612,"enc_right":608,"speed_left_ticks_s":410,"speed_right_ticks_s":406,"pwm_left":88,"pwm_right":91,"param_version":7,"imu_available":false,"imu_quality":"not_configured"}
```

IMU 是可选合同。未核对真实模块、I2C 地址和引脚前，ESP32 必须报告
`imu_available=false`，不能生成伪姿态；配置真实 IMU 后才可额外发送
`imu_yaw_deg`、`yaw_rate_dps` 和 `accel_forward_mps2`。未接 IMU 时格子定位
可以降级运行，但连续车头方向只能标为低置信度，不能通过连续航向验收。

Webots 可发送同名的确定性 IMU 字段，并把 `sim_truth` 放在独立的评估通道。
`sim_truth` 只用于计算定位误差，禁止进入位姿融合器。
为兼容原有 80/500 迷宫墙判断，Webots 另发 `fusion_front_mm`、
`fusion_left_mm`、`fusion_right_mm` 作为连续墙距；融合器优先使用这些字段，
规划器继续读取 `front_mm`、`left_mm`、`right_mm`。

动作完成：

```json
{"type":"done","action_id":"a-0001","name":"move_cell","success":true,"duration_ms":2200,"enc_left":1352,"enc_right":1347,"front_mm":240,"left_mm":160,"right_mm":300}
```

异常：

```json
{"type":"error","action_id":"a-0001","code":"OBSTACLE_TOO_CLOSE","message":"front distance below danger_stop_mm","front_mm":55}
```

## 6. 参数模型

RDK X3 保存完整参数，ESP32 保存运动执行所需参数。Web 页面、自动调参和串口下发都通过 `ParamManager` 统一入口修改。

建议初始 `params.yaml`：

```yaml
robot:
  cell_size_cm: 25
  wheel_diameter_cm: 6.5
  wheel_base_cm: 13.5

motor:
  base_speed: 0.25
  turn_speed: 0.18
  max_speed: 0.40
  min_pwm_left: 45
  min_pwm_right: 45
  max_pwm: 180
  left_trim: 1.00
  right_trim: 1.00

motion:
  cell_ticks: 1350
  turn_90_ticks: 720
  turn_180_ticks: 1440
  brake_ticks: 30
  stop_tolerance_ticks: 20

pid:
  speed_kp: 0.8
  speed_ki: 0.0
  speed_kd: 0.05
  heading_kp: 0.6
  heading_kd: 0.03

tof:
  wall_threshold_mm: 150
  open_threshold_mm: 220
  front_stop_mm: 120
  danger_stop_mm: 60
  filter_window: 5

wall_follow:
  enabled: true
  center_kp: 0.004
  center_max_correction: 0.08

auto_tune:
  enabled: true
  max_change_ratio: 0.15
  max_params_per_step: 3
  tune_after_each_action: true
  tune_after_each_run: true

safety:
  heartbeat_timeout_ms: 500
  action_timeout_ms: 8000
  max_fail_count: 5
  allow_ai_modify_safety: false
```

安全参数不允许自动调参模块修改。Web 页面可以显示安全参数，但修改时需要人工确认，并且 ESP32 必须再次限幅。

## 7. 自动调参设计

第一版自动调参使用规则引擎，输入是动作结果和最近 telemetry，输出是受限参数变更。

### 7.1 分析指标

`MotionAnalyzer` 每次动作完成后生成报告：

```json
{
  "action_id": "a-0001",
  "name": "move_cell",
  "encoder_delta": 5,
  "left_right_ratio": 1.0037,
  "duration_ms": 2200,
  "front_mm": 240,
  "left_mm": 160,
  "right_mm": 300,
  "suspected_issue": "drift_left",
  "confidence": 0.72
}
```

### 7.2 调参规则

- 直行左偏：降低 `right_trim` 或提高 `left_trim`，单次变化不超过 2%。
- 直行右偏：降低 `left_trim` 或提高 `right_trim`，单次变化不超过 2%。
- 每格走短：提高 `motion.cell_ticks`，单次变化不超过 3%。
- 每格走长：降低 `motion.cell_ticks`，单次变化不超过 3%。
- 转弯过头：降低 `motion.turn_90_ticks`，必要时降低 `motor.turn_speed`。
- 转弯不足：提高 `motion.turn_90_ticks`。
- 前方过近触发：提高 `tof.front_stop_mm`，降低 `motor.base_speed`。
- 墙体误判：只调整 `tof.wall_threshold_mm`，每次 5mm。

所有调参变更都记录：

```json
{"type":"param_change","source":"auto_tune","reason":"turn_overshoot","changes":{"motion.turn_90_ticks":[720,690],"motor.turn_speed":[0.18,0.17]}}
```

## 8. 可视化调参系统

RDK X3 上运行 Web 服务，电脑或手机访问：

```text
http://RDK-X3-IP:8000
```

第一版推荐技术：

- 后端：FastAPI + WebSocket
- 前端：单页 HTML/CSS/JS，先不引入大型框架
- 数据：内存状态 + JSONL 日志 + YAML 参数文件

页面结构：

1. 顶部状态栏：连接状态、ESP32 状态、当前动作、急停按钮。
2. 迷宫地图区：当前坐标、朝向、已访问格、墙体、回溯路径。
3. 传感器区：前/左/右距离、左右编码器、PWM、动作耗时。
4. 参数区：基础速度、转弯速度、ticks、PID、墙体阈值，可编辑并下发。
5. 自动调参区：开关、最近一次原因、建议变更、人工接受/撤销。
6. 手动控制区：前进一格、左转、右转、掉头、停止。
7. 日志区：telemetry、done、error、param_change、maze_update。
8. 导出区：导出地图、参数历史、实验日志。

页面设计原则：

- 第一屏直接是调参工作台，不做介绍页。
- 急停按钮始终可见。
- 参数修改必须显示“当前值 -> 新值 -> 是否已下发 -> ESP32 是否 ack”。
- 自动调参建议必须说明原因，不能静默修改关键参数。
- 地图刷新频率 5Hz 左右，串口 telemetry 10Hz-20Hz 即可。

## 9. 运行流程

### 9.1 启动

1. RDK X3 启动 Python 服务。
2. 打开串口，例如 `/dev/ttyUSB0` 或 `/dev/ttyACM0`。
3. 等待 ESP32 发送 `ready`。
4. RDK X3 下发参数并等待 `ack`。
5. ESP32 开始周期发送 telemetry。
6. Dashboard 显示在线状态。

### 9.2 标定

1. 抬起小车，测试左右轮方向和编码器正负。
2. 地面短距离前进，记录 10cm 对应 ticks。
3. 标定一格 `cell_ticks`。
4. 标定原地 90 度 `turn_90_ticks`。
5. 标定前/左/右测距阈值。
6. 保存参数快照。

### 9.3 探索

1. RDK X3 读取当前 telemetry。
2. 根据前/左/右距离判断墙体。
3. 更新 `MazeMap`。
4. `MazePlanner` 选择下一步。
5. RDK X3 发送带 `action_id` 的动作。
6. ESP32 非阻塞状态机执行动作。
7. ESP32 回传 `done` 或 `error`。
8. RDK X3 分析动作结果。
9. `AutoTuner` 在限幅内给出参数变更。
10. Dashboard 展示地图、参数变化和日志。

## 10. 安全机制

ESP32 必须本地实现以下保护，即使 RDK X3 卡死也能停车：

- 心跳超时：超过 `heartbeat_timeout_ms` 没收到心跳，立即停止电机。
- 前方过近：`front_mm < danger_stop_mm`，立即停止并回传 `OBSTACLE_TOO_CLOSE`。
- 动作超时：单个动作超过 `action_timeout_ms`，停止并回传 `ACTION_TIMEOUT`。
- 参数限幅：ESP32 拒绝超过本地范围的参数。
- 急停优先：收到 `estop` 后进入 `ESTOP`，只有人工 `clear_estop` 才允许恢复。

## 11. 实现里程碑

### M0：文档与协议冻结

输出：

- `DEVELOPMENT.md`
- `AGENTS.md`
- 第一版串口协议和参数模型

验收：

- 后续代码改动必须能追溯到本文档。

### M1：ESP32 串口执行层

输出：

- JSON 串口协议
- 非阻塞运动状态机
- telemetry / done / error
- 心跳超时和急停

验收：

- 串口工具发送 `move_cell` 后，小车执行一格并返回 `done`。
- 执行动作期间发送 `estop` 可以及时停车。

### M2：RDK X3 Python 控制层

输出：

- `SerialClient`
- `ParamManager`
- `MazeMap`
- `MazePlanner`
- 基础命令行运行入口

验收：

- RDK X3 能连接 ESP32、下发参数、执行动作、打印地图。
- 没有 ESP32 时可以用模拟串口或 fake client 测试建图逻辑。

### M3：可视化调参 Dashboard

输出：

- FastAPI Web 服务
- WebSocket 实时状态
- 参数编辑和下发
- 手动动作按钮和急停

验收：

- 浏览器可以实时看到测距、编码器、状态和地图。
- 修改参数后能看到 ESP32 ack 和新版本号。

### M4：自动调参与实验日志

输出：

- `MotionAnalyzer`
- `AutoTuner`
- 参数变更日志
- 地图和实验导出

验收：

- 连续动作后能生成明确的调参建议。
- 自动调参不越过 `limits.yaml`。

### M5：视觉终点识别与最短路径复跑

输出：

- 摄像头终点识别
- 终点坐标记录
- BFS/A* 最短路径
- 复跑模式

验收：

- 找到终点后可以停止探索，计算最短路径并复跑。

## 12. 验证策略

无硬件时：

```bash
python3 -m compileall rdk_maze_tuner
python3 -m pytest rdk_maze_tuner/tests -q
```

有 RDK X3 和 ESP32 时：

```bash
python3 rdk_maze_tuner/main.py --serial /dev/ttyUSB0 --baud 115200
python3 rdk_maze_tuner/dashboard/app.py --host 0.0.0.0 --port 8000
```

ESP32 使用 PlatformIO：

```bash
pio run
pio run -t upload
pio device monitor -b 115200
```

硬件验证顺序：

1. 只上电不装轮，确认 telemetry 正常。
2. 抬起小车，确认左右轮方向和编码器正负。
3. 地面慢速前进 10cm，确认编码器闭环。
4. 原地左右转，确认 90 度 ticks。
5. 测试前方挡板触发停车。
6. 测试 Dashboard 急停。
7. 放入简单 2x2 或 3x3 迷宫验证建图。

## 13. 关键假设

- RDK X3 与 ESP32 通过 USB 串口直连，RDK X3 能看到 `/dev/ttyUSB*` 或 `/dev/ttyACM*`。
- ESP32 开发环境使用 VS Code 的 PlatformIO 插件。
- 第一阶段迷宫格子尺寸按 25cm 设计，实际值通过标定修正。
- 当前三路测距为前、左、右；现有代码中的后向测距保留为可选扩展。
- 摄像头第一阶段不参与运动实时闭环。
