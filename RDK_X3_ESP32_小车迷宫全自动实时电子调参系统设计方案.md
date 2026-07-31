# RDK X3 + ESP32 小车迷宫全自动实时电子调参系统设计方案

> 适用硬件：RDK X3 + ESP32 + TT 霍尔编码器电机 + 3 个 VL53LXX-V2 激光测距传感器 + 视觉识别摄像头  
> 目标：实现小车在迷宫中自动探索、构建地图、决策下一步动作，并在运行过程中自动调参，提高运动精度和完成率。

---

## 1. 项目目标

本项目目标是构建一套面向机器人等级考试、AI 实操训练和迷宫探索任务的智能小车系统。

系统希望实现：

1. 小车能够通过 3 个 VL53LXX-V2 激光测距传感器识别前、左、右墙体。
2. 小车能够通过霍尔编码器实现精准前进、转弯和停车。
3. RDK X3 负责迷宫地图构建、路径规划、视觉识别和自动调参。
4. ESP32 负责底层运动执行、电机 PID、传感器读取和安全保护。
5. RDK X3 和 ESP32 通过 USB 串口进行数据传输。
6. 系统支持运行过程中实时修改参数，而不需要重新烧录程序。
7. 系统支持自动分析运动误差，并根据反馈自动调整参数。
8. 后期可扩展为 AI 辅助调参、视觉识别终点、最短路径复跑和实验报告生成系统。

---

## 2. 当前硬件结构

根据现有小车结构，硬件组成如下：

| 模块 | 作用 |
|---|---|
| RDK X3 | 上位机，运行 Linux + Python，负责地图、决策、视觉和自动调参 |
| ESP32 | 下位机，负责电机控制、编码器读取、VL53LXX 测距和动作执行 |
| USB 转串口 | RDK X3 与 ESP32 通信通道 |
| 摄像头 | 视觉识别终点、路标、颜色、二维码或 AprilTag |
| 两个 TT 马达 | 差速驱动，带霍尔编码器 |
| 霍尔编码器 | 测量左右轮实际转动情况，实现精准运动 |
| 3 个 VL53LXX-V2 | 前、左、右激光测距，用于判断墙体和通道 |
| 蓝色底盘 | 小车机械结构 |
| 前部云台/支架 | 固定摄像头，可用于视觉检测 |

---

## 3. 总体设计原则

系统分为两层：

```text
RDK X3：负责“看、想、建图、决策、调参”
ESP32：负责“稳、准、快、安全执行”
```

核心原则：

1. **ESP32 不负责迷宫思考，只负责稳定执行动作。**
2. **RDK X3 不直接高频控制电机，只发送动作命令和参数。**
3. **电机闭环控制必须在 ESP32 内部完成。**
4. **自动调参不等于实时改代码，而是实时修改参数。**
5. **所有可调参数必须外置，不能写死在程序里。**
6. **安全参数不能被 AI 或自动调参模块随意修改。**

---

## 4. 系统总体架构

```text
┌──────────────────────────────────┐
│              RDK X3               │
│ Linux + Python                    │
│                                  │
│ 1. 迷宫地图构建                   │
│ 2. DFS / BFS / A* 决策            │
│ 3. 摄像头视觉识别                 │
│ 4. 参数管理                       │
│ 5. 自动调参算法                   │
│ 6. Web 网页调参界面               │
│ 7. 实验日志                       │
└───────────────┬──────────────────┘
                │ USB 串口
                ↓
┌──────────────────────────────────┐
│              ESP32                │
│ PlatformIO / Arduino Framework    │
│                                  │
│ 1. 电机 PWM 控制                  │
│ 2. 霍尔编码器读取                 │
│ 3. 电机 PID 闭环                  │
│ 4. VL53LXX 测距读取               │
│ 5. 执行动作指令                   │
│ 6. 急停 / 看门狗保护              │
└───────────────┬──────────────────┘
                ↓
┌──────────────────────────────────┐
│       小车底盘 / 电机 / 传感器      │
└──────────────────────────────────┘
```

---

## 5. RDK X3 与 ESP32 的职责分工

### 5.1 RDK X3 职责

RDK X3 是小车的大脑，负责任务级和策略级控制。

主要职责：

1. 串口连接 ESP32。
2. 获取 ESP32 回传的传感器数据。
3. 根据前、左、右测距判断墙体。
4. 构建二维迷宫地图。
5. 使用 DFS / BFS / A* 决策下一步动作。
6. 运行摄像头视觉识别。
7. 管理参数文件。
8. 根据运动反馈自动调参。
9. 保存每一步动作日志。
10. 提供网页调参和实时监控界面。

---

### 5.2 ESP32 职责

ESP32 是小车的身体，负责实时性和底层控制。

主要职责：

1. 接收 RDK X3 发送的动作命令。
2. 控制左右 TT 马达。
3. 读取左右霍尔编码器。
4. 实现左右轮速度 PID 闭环。
5. 读取 3 个 VL53LXX-V2 测距数据。
6. 执行前进一格、左转、右转、掉头、停止等动作。
7. 回传动作结果和实时传感器状态。
8. 实现看门狗断联保护。
9. 实现距离过近自动停车。
10. 执行急停命令。

---

## 6. 为什么不建议 RDK X3 直接控制电机

不推荐：

```text
RDK X3 每 10ms 直接发送左右轮 PWM
```

原因：

1. Linux 不是严格实时系统。
2. Python 运行存在调度延迟。
3. USB 串口通信可能抖动。
4. 视觉识别可能占用算力。
5. 高频电机控制放在上位机容易导致运动不稳定。

推荐方式：

```text
RDK X3 发送动作命令
ESP32 自己闭环执行
```

例如：

```text
RDK X3 → ESP32：
MOVE_CELL 25cm speed=0.28

ESP32：
根据编码器和 PID 控制小车前进 25cm
完成后回传 DONE
```

---

## 7. 自动实时调参的三层逻辑

全自动实时调参应分为三层，不同层级的频率和责任不同。

---

### 7.1 第一层：ESP32 高速闭环调参

位置：ESP32 内部  
频率：100Hz - 200Hz  
对象：左右轮速度、编码器误差、PWM 输出

ESP32 每隔 5ms - 10ms 执行：

```text
读取左轮编码器
读取右轮编码器
计算实际速度
与目标速度比较
PID 计算修正量
输出左右轮 PWM
```

解决问题：

1. 左右轮速度不一致。
2. 直行跑偏。
3. 低速启动困难。
4. 电机负载变化。
5. 地面摩擦变化。

这一层不需要大模型，使用传统 PID 最稳定。

---

### 7.2 第二层：RDK X3 中速运动参数调参

位置：RDK X3  
频率：每完成一格、每完成一次转弯、每到达一个路口  
对象：前进距离、转弯角度、墙体判断阈值、速度参数

例如小车执行前进一格后，ESP32 回传：

```json
{
  "action": "move_cell",
  "success": true,
  "enc_left": 1352,
  "enc_right": 1347,
  "front_mm": 240,
  "left_mm": 160,
  "right_mm": 300,
  "duration_ms": 2200
}
```

RDK X3 根据结果判断：

1. 这一格是否走短。
2. 是否走长。
3. 是否左偏。
4. 是否右偏。
5. 是否撞墙。
6. 是否测距误判。
7. 是否需要降低速度或修改编码器目标值。

---

### 7.3 第三层：RDK X3 慢速 AI 调参

位置：RDK X3  
频率：一轮实验结束后，或连续失败后  
对象：整体策略、参数组合、失败原因

例如一轮迷宫失败后：

```text
失败原因：连续两次右转过头，第三个路口撞墙。
```

AI 或规则引擎给出建议：

```text
1. 降低 turn_speed
2. 减小 turn_90_ticks
3. 增加 front_stop_mm
4. 在路口前增加居中校正
```

第一版建议先使用规则型自动调参，后期再接入大模型分析。

---

## 8. ESP32 内部程序设计

ESP32 程序建议采用模块化结构。

```text
esp32_firmware/
├── src/
│   ├── main.cpp
│   ├── motor.cpp
│   ├── encoder.cpp
│   ├── pid.cpp
│   ├── tof_sensor.cpp
│   ├── serial_protocol.cpp
│   └── safety.cpp
├── include/
│   ├── motor.h
│   ├── encoder.h
│   ├── pid.h
│   ├── tof_sensor.h
│   └── protocol.h
└── platformio.ini
```

你在 VS Code 里看到的“蚂蚁形状”插件，大概率是 **PlatformIO IDE**。ESP32 用 PlatformIO 开发很合适。

---

## 9. ESP32 状态机设计

ESP32 不建议写成简单的阻塞式程序，而应该使用状态机。

```text
IDLE：空闲
MOVING_CELL：前进一格
TURNING_LEFT：左转
TURNING_RIGHT：右转
TURNING_BACK：掉头
ALIGNING：居中校正
STOPPED：停止
ESTOP：急停
ERROR：异常
```

执行逻辑示例：

```text
收到 MOVE_CELL
↓
进入 MOVING_CELL 状态
↓
读取编码器
↓
PID 控制左右轮
↓
达到目标编码器
↓
停车
↓
回传 DONE
↓
回到 IDLE
```

---

## 10. ESP32 任务划分

ESP32 可以划分为 5 个核心任务：

| 任务 | 功能 |
|---|---|
| 编码器任务 | 高频读取左右轮编码器 |
| 电机 PID 任务 | 根据目标速度和实际速度计算 PWM |
| VL53LXX 任务 | 读取前、左、右测距数据 |
| 串口通信任务 | 接收 RDK X3 命令，回传状态 |
| 安全任务 | 断联停车、距离过近停车、急停保护 |

---

## 11. RDK X3 Python 项目结构

RDK X3 上建议使用 Python 直接开发，项目结构如下：

```text
rdk_maze_ai_tuner/
├── main.py
├── config/
│   ├── params.yaml
│   └── limits.yaml
├── core/
│   ├── serial_client.py
│   ├── param_manager.py
│   ├── maze_map.py
│   ├── maze_planner.py
│   ├── motion_analyzer.py
│   ├── auto_tuner.py
│   ├── vision_detector.py
│   ├── logger.py
│   └── safety_guard.py
├── dashboard/
│   ├── app.py
│   ├── templates/
│   └── static/
├── logs/
├── videos/
└── reports/
```

---

## 12. RDK X3 主循环设计

RDK X3 的主循环逻辑：

```python
while running:
    # 1. 从 ESP32 获取传感器数据
    sensor = esp32.get_sensor()

    # 2. 判断当前格子的墙体情况
    walls = maze_map.detect_walls(sensor)

    # 3. 更新迷宫地图
    maze_map.update(current_cell, current_direction, walls)

    # 4. 根据地图和策略选择下一步
    action = planner.next_action(maze_map, current_cell, current_direction)

    # 5. 发送动作给 ESP32
    result = esp32.execute(action)

    # 6. 分析本次动作执行效果
    motion_report = motion_analyzer.analyze(result)

    # 7. 自动调参
    new_params = auto_tuner.tune(motion_report)

    # 8. 下发新参数
    esp32.set_params(new_params)

    # 9. 记录日志
    logger.save(sensor, action, result, new_params)
```

核心思想：

```text
RDK X3 控制动作和参数，不直接控制电机 PWM。
```

---

## 13. 迷宫地图构建逻辑

建议采用规则格子迷宫。

假设每格 25cm：

```yaml
maze:
  cell_size_cm: 25
```

每到一个格子中心，读取 3 个方向距离：

```text
front_distance
left_distance
right_distance
```

判断墙体：

```text
front_distance < wall_threshold → 前方有墙
left_distance < wall_threshold → 左侧有墙
right_distance < wall_threshold → 右侧有墙
```

例如：

```json
{
  "cell": [2, 3],
  "direction": "N",
  "walls": {
    "front": true,
    "left": false,
    "right": true,
    "back": false
  }
}
```

然后根据当前朝向，把相对方向转换为地图中的绝对方向：

```text
N：北
E：东
S：南
W：西
```

---

## 14. 迷宫探索算法

第一版建议使用 DFS 探索 + 回溯。

### 14.1 DFS 基本逻辑

```text
当前位置
↓
检测前、左、右墙体
↓
把墙体信息写入地图
↓
优先选择没有探索过的方向
↓
如果没有未探索方向，则回退到上一个格子
↓
直到找到终点或完成全图探索
```

---

### 14.2 每个格子保存的数据

```python
cell = {
    "x": 0,
    "y": 0,
    "walls": {
        "N": False,
        "E": True,
        "S": False,
        "W": True
    },
    "visited": True,
    "parent": [0, 1]
}
```

---

### 14.3 找到终点后的流程

终点可以由摄像头识别，例如：

1. 红色区域。
2. 二维码。
3. AprilTag。
4. 特定图案。
5. YOLO 识别目标。

找到终点后：

```text
摄像头识别终点
↓
记录终点坐标
↓
使用 BFS / A* 计算起点到终点的最短路径
↓
小车按照最短路径复跑
```

完整流程：

```text
探索建图 → 找到终点 → 计算最短路径 → 稳定复跑
```

---

## 15. 需要实时调节的参数

### 15.1 电机基础参数

```yaml
motor:
  base_speed: 0.28
  turn_speed: 0.22
  max_speed: 0.45
  min_pwm_left: 45
  min_pwm_right: 48
  left_trim: 1.00
  right_trim: 1.00
```

作用：

| 参数 | 作用 |
|---|---|
| base_speed | 小车直行基础速度 |
| turn_speed | 小车转弯速度 |
| max_speed | 最高速度限制 |
| min_pwm_left | 左电机最低启动 PWM |
| min_pwm_right | 右电机最低启动 PWM |
| left_trim | 左轮补偿 |
| right_trim | 右轮补偿 |

---

### 15.2 编码器运动参数

```yaml
motion:
  cell_ticks: 1350
  turn_90_ticks: 720
  turn_180_ticks: 1440
  brake_ticks: 30
  stop_tolerance_ticks: 15
```

作用：

| 参数 | 作用 |
|---|---|
| cell_ticks | 前进一格所需编码器脉冲 |
| turn_90_ticks | 原地转 90 度所需编码器脉冲 |
| turn_180_ticks | 原地转 180 度所需编码器脉冲 |
| brake_ticks | 提前减速距离 |
| stop_tolerance_ticks | 停车误差容许范围 |

---

### 15.3 PID 参数

```yaml
pid:
  speed_kp: 0.8
  speed_ki: 0.0
  speed_kd: 0.05
  heading_kp: 0.6
  heading_kd: 0.03
```

第一版建议：

```text
先调 speed_kp、speed_kd
speed_ki 暂时保持 0
```

---

### 15.4 VL53LXX 墙体判断参数

```yaml
tof:
  wall_threshold_mm: 150
  open_threshold_mm: 220
  front_stop_mm: 120
  filter_window: 5
  sensor_offset_left_mm: 0
  sensor_offset_right_mm: 0
```

作用：

| 参数 | 作用 |
|---|---|
| wall_threshold_mm | 小于该距离认为有墙 |
| open_threshold_mm | 大于该距离认为有通道 |
| front_stop_mm | 前方小于该距离需要停车 |
| filter_window | 测距滤波窗口 |
| sensor_offset_left_mm | 左测距安装补偿 |
| sensor_offset_right_mm | 右测距安装补偿 |

---

### 15.5 居中控制参数

当左右两侧都有墙时，小车可以根据左右距离差自动居中。

```yaml
wall_follow:
  enabled: true
  center_kp: 0.004
  center_max_correction: 0.08
```

逻辑：

```text
左侧距离 - 右侧距离 = 横向偏差
偏差大 → 修改左右轮速度
```

示例：

```text
左边太近，右边太远 → 小车向右修正
右边太近，左边太远 → 小车向左修正
```

---

### 15.6 自动调参限制参数

```yaml
auto_tune:
  enabled: true
  max_change_ratio: 0.15
  max_params_per_step: 3
  allow_motor_tune: true
  allow_tof_tune: true
  allow_pid_tune: true
  allow_safety_tune: false
```

原则：

```text
自动调参可以调整控制参数，但不能调整安全参数。
```

---

## 16. 自动调参规则设计

自动调参模块建议先采用规则引擎，后续再接入大模型。

---

### 16.1 直行偏移调参

现象：前进一格后小车左偏。

可能原因：

1. 右轮速度偏大。
2. 左轮速度偏小。
3. 左右轮编码器比例不准。
4. 地面摩擦不均。

调参策略：

```python
if drift_direction == "left":
    params["motor"]["right_trim"] *= 0.98
    params["motor"]["left_trim"] *= 1.01
```

现象：前进一格后小车右偏。

调参策略：

```python
if drift_direction == "right":
    params["motor"]["left_trim"] *= 0.98
    params["motor"]["right_trim"] *= 1.01
```

---

### 16.2 前进距离调参

现象：每格走短了。

```python
if move_error_cm > 1.0:
    params["motion"]["cell_ticks"] *= 1.03
```

现象：每格走长了。

```python
if move_error_cm < -1.0:
    params["motion"]["cell_ticks"] *= 0.97
```

---

### 16.3 转弯调参

现象：转弯过头。

```python
if turn_error == "overshoot":
    params["motion"]["turn_90_ticks"] *= 0.96
    params["motor"]["turn_speed"] *= 0.95
```

现象：转弯不足。

```python
if turn_error == "undershoot":
    params["motion"]["turn_90_ticks"] *= 1.04
```

---

### 16.4 撞墙调参

现象：前方撞墙或停太晚。

```python
if collision_count > 0:
    params["tof"]["front_stop_mm"] += 10
    params["motor"]["base_speed"] *= 0.92
```

---

### 16.5 路口误判调参

现象：明明有路，却判断成墙。

```python
if false_wall_detected:
    params["tof"]["wall_threshold_mm"] -= 5
```

现象：明明是墙，却判断成有路。

```python
if false_open_detected:
    params["tof"]["wall_threshold_mm"] += 5
```

---

## 17. RDK X3 与 ESP32 串口协议

建议第一版使用换行 JSON 协议，方便调试和日志记录。

---

### 17.1 RDK X3 发给 ESP32

#### 设置单个参数

```json
{"cmd":"set_param","name":"base_speed","value":0.28}
```

#### 批量设置参数

```json
{
  "cmd": "set_params",
  "params": {
    "base_speed": 0.28,
    "turn_speed": 0.22,
    "cell_ticks": 1350,
    "turn_90_ticks": 720,
    "speed_kp": 0.8,
    "speed_kd": 0.05
  }
}
```

#### 前进一格

```json
{"cmd":"move_cell","speed":0.28,"cell_ticks":1350}
```

#### 左转 90 度

```json
{"cmd":"turn","angle":-90,"speed":0.22,"ticks":720}
```

#### 右转 90 度

```json
{"cmd":"turn","angle":90,"speed":0.22,"ticks":720}
```

#### 停止

```json
{"cmd":"stop"}
```

#### 急停

```json
{"cmd":"estop"}
```

---

### 17.2 ESP32 回给 RDK X3

#### 实时状态

```json
{
  "type": "telemetry",
  "state": "MOVING_CELL",
  "front_mm": 320,
  "left_mm": 180,
  "right_mm": 260,
  "enc_left": 1250,
  "enc_right": 1240,
  "pwm_left": 88,
  "pwm_right": 91,
  "battery_mv": 7400
}
```

#### 动作完成

```json
{
  "type": "done",
  "action": "move_cell",
  "success": true,
  "enc_left": 1352,
  "enc_right": 1347,
  "duration_ms": 2200,
  "front_mm": 240,
  "left_mm": 160,
  "right_mm": 300
}
```

#### 异常信息

```json
{
  "type": "error",
  "code": "OBSTACLE_TOO_CLOSE",
  "front_mm": 65
}
```

---

## 18. 通信频率建议

| 模块 | 建议频率 |
|---|---|
| ESP32 电机 PID | 100Hz - 200Hz |
| ESP32 读取 VL53LXX | 20Hz - 50Hz |
| ESP32 回传状态 | 10Hz - 20Hz |
| RDK X3 决策 | 每完成一个动作后 |
| RDK X3 自动调参 | 每完成一个动作或每轮实验后 |
| 网页刷新 | 5Hz - 10Hz |

串口波特率：

```text
调试阶段：115200
稳定阶段：460800 或 921600
```

---

## 19. VL53LXX 三传感器注意事项

三个 VL53LXX 通常默认 I2C 地址相同，因此需要处理地址冲突。

### 19.1 方案一：XSHUT 分别唤醒

每个 VL53LXX 的 XSHUT 接到不同 GPIO。

启动时依次唤醒并修改地址：

```text
左传感器 → 0x30
前传感器 → 0x31
右传感器 → 0x32
```

---

### 19.2 方案二：I2C 多路复用器

使用 TCA9548A。

```text
通道 0：左传感器
通道 1：前传感器
通道 2：右传感器
```

如果三个测距数据偶尔乱跳，需要重点检查：

1. I2C 地址是否冲突。
2. 线长是否过长。
3. 电源是否稳定。
4. 传感器是否需要滤波。
5. 是否存在强反光或黑色吸光墙面。

---

## 20. 摄像头的使用建议

第一版不建议依赖摄像头完成迷宫主体任务。

第一版主传感器：

```text
3 个 VL53LXX + 编码器
```

用于完成：

1. 墙体判断。
2. 格子前进。
3. 转弯。
4. 建图。
5. DFS 探索。
6. 自动调参。

摄像头后续用于：

1. 终点识别。
2. 颜色区域识别。
3. 二维码识别。
4. AprilTag 识别。
5. 视觉纠偏。
6. 视频记录。
7. AI 视觉训练。

---

## 21. 推荐完整运行流程

### 21.1 启动阶段

```text
1. RDK X3 启动 Python 主程序
2. 打开 USB 串口连接 ESP32
3. ESP32 回传 READY
4. RDK X3 下发默认参数
5. 读取三路 VL53LXX 初始值
6. 检查编码器是否正常
7. 检查摄像头是否正常
8. 进入 IDLE
```

---

### 21.2 自动校准阶段

```text
1. 左右轮空转测试
2. 编码器方向确认
3. 前进短距离测试
4. 计算左右轮补偿
5. 转弯 90 度测试
6. 初步修正 turn_90_ticks
7. 保存校准参数
```

---

### 21.3 迷宫探索阶段

```text
1. 小车进入第一个格子中心
2. 读取前、左、右距离
3. 判断当前格子的墙体
4. 更新地图
5. DFS 选择下一步
6. ESP32 执行前进或转弯
7. RDK X3 分析执行误差
8. 自动修改参数
9. 继续探索
```

---

### 21.4 找到终点后

```text
1. 摄像头识别终点标志
2. 记录终点坐标
3. 使用 BFS / A* 计算最短路径
4. 小车按照最短路径复跑
5. 保存完整地图和参数变化记录
```

---

## 22. 网页调参和监控界面设计

RDK X3 上启动 Web 服务，电脑或手机访问：

```text
http://RDK-X3-IP:8000
```

页面建议包含：

1. 当前小车状态。
2. 前、左、右距离。
3. 左右编码器。
4. 当前坐标和朝向。
5. 当前迷宫地图。
6. 当前参数。
7. 自动调参开关。
8. 手动急停按钮。
9. 最近一次调参原因。
10. 运行日志。

---

### 22.1 参数显示区

显示并允许修改：

```text
base_speed
turn_speed
cell_ticks
turn_90_ticks
left_trim
right_trim
speed_kp
speed_kd
wall_threshold_mm
front_stop_mm
center_kp
```

---

### 22.2 AI / 自动调参建议区

示例：

```text
本次问题：右转过头

修改参数：
turn_90_ticks: 720 → 690
turn_speed: 0.22 → 0.20

原因：
连续两次右转后右侧距离过近，推断转弯过冲。
```

---

## 23. 安全机制设计

全自动实时调参必须加入安全机制。

---

### 23.1 ESP32 断联停车

```text
如果 500ms 没收到 RDK X3 心跳，
ESP32 立即停止电机。
```

---

### 23.2 距离过近停车

```text
front_mm < 60
立即停车
```

---

### 23.3 参数限幅

```yaml
limits:
  base_speed: [0.10, 0.45]
  turn_speed: [0.08, 0.35]
  speed_kp: [0.0, 2.0]
  speed_kd: [0.0, 0.5]
  wall_threshold_mm: [80, 260]
  front_stop_mm: [80, 250]
```

---

### 23.4 单次调参幅度限制

```text
每次最多修改 3 个参数。
每个参数变化不超过 15%。
连续失败 3 次自动降速。
连续失败 5 次自动停止，等待人工检查。
```

---

### 23.5 物理急停

建议加一个物理急停按钮，直接切断电机驱动使能端。

物理急停优先级高于软件命令。

---

## 24. 第一阶段最小可行版本

第一阶段目标：

```text
小车能自动探索迷宫，并在过程中自动调整基础参数。
```

第一阶段功能清单：

1. ESP32 能读取编码器。
2. ESP32 能读取三个 VL53LXX。
3. ESP32 能执行 move_cell / turn_left / turn_right / stop。
4. RDK X3 能通过串口发送动作。
5. RDK X3 能接收 ESP32 telemetry。
6. RDK X3 能建立二维迷宫地图。
7. RDK X3 能用 DFS 决策。
8. RDK X3 能根据执行结果自动调整：
   - base_speed
   - turn_speed
   - cell_ticks
   - turn_90_ticks
   - left_trim
   - right_trim
   - wall_threshold_mm
   - front_stop_mm

---

## 25. 推荐初始参数

可以先建立 `params.yaml`：

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

maze:
  strategy: dfs
  start_x: 0
  start_y: 0
  start_dir: N
  allow_backtrack: true

auto_tune:
  enabled: true
  max_change_ratio: 0.15
  max_params_per_step: 3
  tune_after_each_action: true
  tune_after_each_run: true

safety:
  heartbeat_timeout_ms: 500
  max_run_time_sec: 300
  max_fail_count: 5
  allow_ai_modify_safety: false
```

注意：

```text
cell_ticks 和 turn_90_ticks 只是初始估算值，必须根据实际编码器标定。
```

---

## 26. 推荐开发顺序

请按以下顺序开发：

```text
第一步：ESP32 单独测试左右电机和编码器
第二步：ESP32 实现 PID 速度闭环
第三步：ESP32 测试三个 VL53LXX
第四步：RDK X3 通过串口读取 ESP32 telemetry
第五步：RDK X3 发送 move_cell / turn 命令
第六步：实现前进一格、转弯 90 度
第七步：实现 RDK X3 参数下发
第八步：实现网页实时看参数、改参数
第九步：实现 DFS 迷宫建图
第十步：实现自动调参规则
第十一步：接入摄像头识别终点
第十二步：加入 AI 分析和实验报告
```

---

## 27. 最终运行效果

最终系统应实现：

```text
小车进入迷宫
↓
RDK X3 读取前、左、右墙体
↓
构建迷宫地图
↓
判断下一步走哪
↓
ESP32 精准执行动作
↓
RDK X3 分析动作误差
↓
自动调整速度、编码器、转弯、墙体阈值
↓
继续探索
↓
摄像头发现终点
↓
计算最短路径
↓
复跑最优路线
↓
生成完整地图、调参日志、实验报告
```

---

## 28. 一句话总结

这套系统的正确架构是：

```text
ESP32 做高速稳定闭环；
RDK X3 做地图、决策和自动调参；
USB 串口负责动作与参数同步；
摄像头用于终点和高级视觉识别；
日志系统负责实验复盘和 AI 调参依据。
```

这样既安全、稳定，又真正适合做电子学会机器人八级训练、AI 实操调参课程和学生科创作品。
