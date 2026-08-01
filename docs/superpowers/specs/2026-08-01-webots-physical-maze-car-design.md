# Webots 物理迷宫小车优先重建设计

日期：2026-08-01

状态：用户已确认，批准进入实施计划

适用项目：RDK X3 + ESP32 迷宫小车

实施策略：物理仿真优先，完成后再继续扩展任务逻辑

## 1. 文档定位

本文定义如何把现有 Webots 动作级动画升级为可用于闭环控制、滑移分析和参数校准的物理仿真。

本次变更不推翻已经完成的网站、账户、控制权、任务状态机、地图版本、定位融合、成绩和回放模块。它只替换 Webots 模式下的底层运动与传感器来源，并保持现有一行一个 JSON 的设备协议。

正式实施前的当前证据为：

- `maze_world.wbt` 中的左右轮只是两个静态圆柱，没有车轮关节、电机或编码器设备。
- 当前 Webots 控制器通过 Supervisor 直接写 `translation` 和 `rotation` 实现移动。
- 当前前/左/右距离、编码器和 IMU 由确定性 `MazeSimEngine` 计算，不是 Webots 物理设备输出。
- 现有确定性引擎适合协议、任务和算法单元测试，但不能证明轮胎摩擦、空转、碰撞、质量、重心和真实闭环行为。
- 当前参数中的 250 mm 单格距离与内置 Webots 地图的 450 mm 单格尺寸不一致；物理模式不能继续把两者都解释为“一格”。

## 2. 已确认目标

第一版物理仿真必须做到：

1. 车辆依靠两个主动轮和一个被动万向轮在 Webots 刚体世界中运动。
2. 左右轮分别具有 `HingeJoint`、`RotationalMotor` 和 `PositionSensor`。
3. 前、左、右三向距离来自三个独立 `DistanceSensor`。
4. 姿态和运动证据来自 `InertialUnit`、`Gyro` 和 `Accelerometer`。
5. Python 控制器以 8 ms 周期复刻 ESP32 的动作状态机、安全检查和 PID 闭环。
6. 质量、重心、惯量、电机响应、编码器、ToF 和地面摩擦均可配置。
7. 正常、低摩擦、左右不对称和局部低摩擦场景能够产生真实可观察的轨迹差异。
8. 网站仍通过现有 `DeviceSession` 和 TCP JSON 协议控制仿真。
9. Webots 真值只用于评分、误差评估和校准，不进入定位与运动控制。
10. 每次运行保存地图版本、参数版本和物理配置摘要，保证成绩和回放可复现。

## 3. 非目标

第一版明确不做：

- 不把真实 ESP32 固件编译进 Webots。
- 不要求实体 ESP32 在线，不采用硬件在环作为默认路径。
- 不模拟 VL53L0X 内部光学电路、TT 电机电磁细节或电池化学模型。
- 不让 RDK、网站或服务器以高频率直接发送左右轮 PWM。
- 不使用 `sim_truth` 修正控制器、定位融合器或迷宫决策。
- 不因为仿真通过而宣称真实接线、电机、传感器或实车路线已经验收。
- 不在本阶段继续增加新的任务策略、强化学习或大模型自动调参逻辑。

## 4. 核心架构

```text
浏览器 / TaskOrchestrator
        │ action + action_id
        ▼
DeviceSession / TCP 127.0.0.1:8765
        ▼
SimProtocolServer
        ▼
PhysicalMazeEngine
├── 协议校验和动作上下文
├── 非阻塞动作状态机
├── 8 ms PID / 安全控制
├── PhysicalDeviceAdapter
└── PhysicalTelemetryProvider
        ▼
Webots PhysicalMazeCar
├── 左右 HingeJoint
├── RotationalMotor
├── PositionSensor
├── 三向 DistanceSensor
├── InertialUnit / Gyro / Accelerometer
├── 被动万向轮
└── Physics / ContactProperties
        │
        ├── 普通设备观测 ──► telemetry / done / error
        └── Supervisor 真值 ──► TruthObserver ──► 评分与回放
```

模块边界：

- `PhysicalMazeEngine` 只管理动作、闭环、安全和统一协议，不操作网页。
- `PhysicalDeviceAdapter` 是读取 Webots 设备和写入电机力矩的唯一入口。
- `PhysicalTelemetryProvider` 只产生允许进入普通 telemetry 的设备证据。
- `TruthObserver` 单独读取刚体位置、速度和接触结果，只产生 `sim_truth` 和评估指标。
- `MazeMap`、`MazePlanner`、`PoseFusion`、`SlipEstimator` 不直接依赖 Webots API。
- `SimProtocolServer` 保持传输职责，不内嵌运动算法。

现有确定性 `MazeSimEngine` 保留，继续作为无 Webots、速度快、结果可重复的单元测试后端。物理 Webots 成为正式可视化仿真后端，两者使用相同 JSON 合同。

## 5. 车辆物理模型

### 5.1 坐标约定

车辆局部坐标约定：

- `+x`：车辆右侧。
- `+y`：车辆上方。
- `-z`：车辆前方。
- 航向角继续使用项目现有的北、东、南、西约定，由协议适配层转换。

除执行重置外，控制器禁止直接修改整车 `translation` 或 `rotation`。重置时允许 Supervisor 设置起始位姿并调用 `resetPhysics()`，随后所有运动必须来自车轮与地面的物理作用。

### 5.2 第一版几何基线

已确认或推导的固定基线：

| 参数 | 初值 | 状态 |
| --- | ---: | --- |
| 轮径 | 65 mm | 用户确认 |
| 轮半径 | 32.5 mm | 由轮径推导 |
| 左右轮中心距 | 135 mm | 用户确认 |
| 单轮宽度 | 26 mm | 照片比例初值 |
| 底盘长度 | 230 mm | 照片比例初值 |
| 底盘宽度 | 160 mm | 照片比例初值 |
| 总质量 | 1.20 kg | 待实车称重校准 |
| 车轮质量 | 0.06 kg/个 | 待校准 |
| 主体等效质量 | 1.08 kg | 使总质量保持 1.20 kg |
| 等效重心 | `[0, 0.070, 0.010]` m | 偏高、略偏后的照片估计 |

视觉模型表现蓝色底盘、两侧 TT 轮、前部摄像头、多层电子板和前万向轮。碰撞模型保持简单、稳定和可计算：底盘、电子设备等效体、左右轮和万向轮分别有清晰的 `boundingObject`，不以复杂网格作为主要碰撞体。

### 5.3 刚体与万向轮

- 主体和左右轮都使用明确 `Physics` 参数。
- 主体使用显式质量、重心和惯量；第一版惯量由简化长方体和质量分布计算，配置中保存最终值。
- 左右轮使用独立刚体，轴线与轮距严格对应。
- 前部采用被动球形或低阻小轮万向支撑，不设置主动电机。
- 万向轮必须能支撑底盘，不能造成静止漂移、持续弹跳或转向锁死。

## 6. 物理配置

新增独立物理配置文件，作为每次仿真运行的不可变输入。配置分为：

```yaml
geometry:
  wheel_radius_m: 0.0325
  wheel_width_m: 0.026
  axle_track_m: 0.135
  chassis_length_m: 0.230
  chassis_width_m: 0.160

body:
  total_mass_kg: 1.20
  wheel_mass_kg: 0.06
  center_of_mass_m: [0.0, 0.070, 0.010]

motor:
  max_velocity_rad_s: 20.0
  max_torque_nm: 0.60
  response_time_s: 0.08
  pwm_dead_zone: 0.18
  left_gain: 1.0
  right_gain: 1.0

encoder:
  ticks_per_revolution: 1103
  quantization_enabled: true
  missed_pulse_rate: 0.0

tof:
  min_range_m: 0.03
  max_range_m: 2.0
  field_of_view_deg: 25.0
  noise_std_mm: 5.0
  dropout_rate: 0.005

imu:
  yaw_noise_std_deg: 0.35
  gyro_noise_std_dps: 0.5
  accel_noise_std_mps2: 0.05

surface:
  profile: normal
```

编码器初值由当前参数反推：

```text
250 mm / 1350 ticks
65 mm 轮径对应周长约 204.2 mm
每圈 ticks 约为 1103
```

这些数值是仿真初值，不是实测证明。后续实车称重、尺寸测量和动作标定只替换物理配置版本，不改变协议和上层任务接口。

## 7. 地面与摩擦场景

车辆轮胎和地面使用明确的 `contactMaterial` 与 `ContactProperties`。第一版包含四个可选择配置：

| 场景 | 初始等效摩擦设置 | 预期表现 |
| --- | --- | --- |
| normal | 左右轮 0.90 | 稳定直行和转向 |
| low | 左右轮 0.25 | 制动距离增加、明显空转 |
| asymmetric | 左轮 0.35、右轮 0.90 | 持续航向偏差 |
| local_patch | 正常地面内局部区域 0.25 | 进入区域后瞬态打滑 |

这些系数是 Webots 接触模型的配置值，不宣称等于实车轮胎的实验摩擦系数。

左右轮使用独立的接触材质名称，使 asymmetric 场景能够分别配置；local_patch 使用独立地面碰撞区域和接触材质，不通过修改控制器数据伪造滑移。

物理配置只能在任务重置前选择。运行中禁止静默改变质量、重心、摩擦或电机物理参数。每次运行记录物理配置版本和内容摘要。

## 8. 电机、编码器和模拟 ESP32 闭环

### 8.1 控制周期

- `WorldInfo.basicTimeStep` 调整为 8 ms。
- 模拟 ESP32 每个 Webots step 执行一次控制 tick，约 125 Hz。
- telemetry 默认每 50 ms 产生一次，约 20 Hz。
- 浏览器渲染目标保持 24 FPS，渲染频率与控制频率解耦。

### 8.2 电机控制

每侧车轮流程为：

```text
动作目标
→ 目标轮速/目标编码器
→ 速度 PID + 左右同步/航向修正
→ PWM 等效值与死区
→ 一阶电机响应
→ 受最大扭矩限制的轮轴力矩
→ Webots 刚体与接触求解
```

控制器使用 `PositionSensor` 的角度差计算真实轮速。PID 输出经过 `max_torque_nm`、`pwm_dead_zone`、左右增益和参数限幅，再作用到对应 `RotationalMotor`。不得直接把理论位移写回车体。

### 8.3 动作状态机

低层状态至少包含：

```text
IDLE
MOVING_CELL
TURNING_LEFT
TURNING_RIGHT
TURNING_BACK
SETTLING
PAUSED
ESTOP
ERROR
```

收到动作后：

1. 校验 `action_id`、当前状态、目标参数和安全条件。
2. 立即返回匹配 `seq` 的 ACK。
3. 记录起始编码器、IMU 航向、ToF、时间和动作目标。
4. 每 8 ms 执行传感器读取、安全检查和 PID。
5. 达到位置或角度目标后进入 `SETTLING`，受控减速。
6. 连续多个周期满足位置、角度和低速阈值后返回 `done`。
7. 任何异常返回同一 `action_id` 的 `error`，地图不得推进。

### 8.4 前进与转向

前进一格：

- 以左右编码器平均值作为距离主证据。
- 使用左右编码器差和 IMU 航向保持直行。
- 有墙时允许有限的墙面居中修正。
- 到达目标前按剩余 ticks 生成减速曲线。
- 物理执行器严格执行命令中的 `target_ticks`，不自行假设地图格长。
- P3 使用 250 mm 直线标定动作；P5 再由上层根据当前地图尺寸和标定的 ticks/mm 生成每个方向的 `target_ticks`。

原地转向：

- 左右轮使用相反方向目标。
- 编码器目标为主，IMU 航向用于收敛和停止判断。
- 完成判断同时检查轮速降低和角度稳定。

控制器只允许消费编码器、ToF、IMU 和动作状态，禁止消费 Supervisor 位姿或 `sim_truth`。

## 9. 传感器设计

### 9.1 编码器

- 左右 `PositionSensor` 分别安装在车轮关节。
- 原始角度转换为量化 ticks。
- telemetry 同时保留轮角度、轮速和换算后的编码器计数。
- 可选的量化、丢脉冲和方向错误只由物理配置显式启用。

### 9.2 三向 VL53L0X

- 前、左、右分别使用一个 `DistanceSensor`，安装位姿独立配置。
- 量程初值为 30–2000 mm，视场角初值为 25°。
- `raw_front_mm`、`raw_left_mm`、`raw_right_mm` 保存 Webots 设备读数。
- 普通 `front_mm`、`left_mm`、`right_mm` 是模拟 ESP32 滤波、范围校验和异常处理后的值。
- 噪声和 dropout 使用固定运行种子，保证同一物理配置可重复。

### 9.3 IMU

- `InertialUnit` 产生姿态。
- `Gyro` 产生角速度。
- `Accelerometer` 产生线加速度。
- 普通 telemetry 使用加入已配置噪声后的观测。
- Supervisor 真值姿态只进入 `sim_truth`。

## 10. telemetry 与真值隔离

telemetry 分为三层：

1. `raw_*`：Webots 设备原始读数。
2. 普通设备字段：经过模拟 ESP32 换算、滤波和质量判定的数据。
3. `sim_truth`：独立评估通道中的真实刚体位姿、速度、轮地滑移和接触结果。

普通 telemetry 至少新增或明确：

```text
wheel_angle_left_rad / wheel_angle_right_rad
wheel_speed_left_rad_s / wheel_speed_right_rad_s
raw_front_mm / raw_left_mm / raw_right_mm
imu_yaw_deg / yaw_rate_dps / accel_forward_mps2
pwm_left / pwm_right
motor_torque_left_nm / motor_torque_right_nm
controller_period_ms
friction_profile
quality_flags
```

`sim_truth` 至少包含：

```text
x_mm / y_mm / yaw_deg
linear_speed_mps / angular_speed_dps
left_slip_rate / right_slip_rate
active_surface_profile
collision_count
```

协议解析继续使用现有白名单策略，确保 `sim_truth` 不会进入 `PoseFusion` 输入。

## 11. 暂停、停止和安全

### 11.1 暂停

网站任务级 `pause` 保持既有安全边界语义：

- 已下发的最小动作安全完成后不再发送下一动作。
- 车辆稳定停车后任务进入 `PAUSED`。

如果低层仿真直接收到动作中的 `pause`，则受控减速并以 `PAUSED` 结束当前动作，不回传成功 `done`。

### 11.2 停止

`stop` 立即启动受控减速，当前动作返回 `STOPPED`。车辆稳定后回到 `IDLE`，地图不推进。

### 11.3 急停

`estop` 具有最高优先级：

- 立即把驱动目标与力矩降为零并施加制动策略。
- 取消当前动作并返回 `ESTOP`。
- 保持 `ESTOP` 状态，必须显式 `clear_estop` 和重新预检后才能继续。

### 11.4 本地安全和故障

模拟 ESP32 独立处理：

- 心跳超时。
- 前方危险距离。
- 动作超时。
- 由 ToF、IMU 冲击和编码器停滞共同推断的疑似碰撞。
- 失速。
- 持续轮子空转。
- 参数越界。
- 非有限传感器或物理数值。

建议错误码：

```text
OBSTACLE_TOO_CLOSE
ACTION_TIMEOUT
HEARTBEAT_TIMEOUT
COLLISION_SUSPECTED
MOTOR_STALL
WHEELSPIN_PERSISTENT
STOPPED
PAUSED
ESTOP
SIM_DEVICE_MISSING
SIM_CONFIG_INVALID
SIM_PHYSICS_ERROR
MAP_GEOMETRY_UNSAFE
```

缺少左右任一电机、编码器或前/左/右任一核心 ToF 时，预检必须返回 `SIM_DEVICE_MISSING` 并保持电机归零。

模拟 ESP32 不读取 Supervisor 接触真值，因此不能把真实接触直接伪装成车载碰撞传感器。`TruthObserver` 记录的实际 `collision_count` 只用于评分；控制侧只能根据现有 ToF、IMU 和电机/编码器证据返回 `COLLISION_SUSPECTED`、`MOTOR_STALL` 或 `OBSTACLE_TOO_CLOSE`。

## 12. 重置语义

物理仿真的 `reset` 必须是原子操作：

1. 禁止新动作并将电机归零。
2. 清除当前动作和所有低层控制积分。
3. 使用已选地图版本重建碰撞墙体。
4. 使用已选物理配置设置质量、重心、电机和接触参数。
5. 将车辆放回地图起点并调用 `resetPhysics()`。
6. 清除轮速、编码器累计、ToF/IMU 滤波历史、急停和错误状态。
7. 重置任务时间、轨迹和评估统计。
8. 发布新的 `ready` 与 `telemetry`，其中包含地图、参数和物理配置摘要。

重置是唯一允许 Supervisor 直接设置整车位姿的正常业务操作。

## 13. 滑移与摩擦评估

滑移分为控制侧估算和独立真值评估：

- 控制侧只用编码器、IMU 和墙距变化估算左右轮滑移，允许生成有限的速度、左右轮和航向修正。
- `TruthObserver` 使用车轮角速度、轮半径和刚体对地速度计算真实滑移率。
- 真值只用于评分、图表、回放和校准控制侧估算误差。
- 自动修正不得修改急停、心跳、危险距离、最大 PWM 或其他安全参数。

物理场景必须能稳定复现：

- 正常地面低滑移。
- 低摩擦地面双轮空转。
- 左右不对称摩擦导致的航向漂移。
- 进入局部低摩擦区域后的瞬态变化。

## 14. 地图与墙体

现有地图版本和 `WebotsMapLoader` 继续使用。生成的每段墙体必须同时具有：

- 可见几何。
- `boundingObject`。
- 明确高度与厚度。
- 与 ToF 和碰撞系统一致的坐标。

地图加载失败、摘要不匹配或墙体构建失败时，车辆不得开始运动。重置后应验证起点没有与墙体或地面穿插。

### 14.1 格尺寸与动作距离

250 mm 是当前运动参数的标定距离，不再被物理模式硬编码为所有地图的单格尺寸。物理任务使用保存地图中的实际尺寸：

- 南北移动使用 `cell_height_mm`。
- 东西移动使用 `cell_width_mm`。
- 上层按当前编码器标定换算为动作的 `target_ticks`。
- 当前 1350 ticks / 250 mm 的初值等于 5.4 ticks/mm。
- 例如 450 mm 格长的初始目标为 2430 ticks，而不是 1350 ticks。

`robot.cell_size_cm` 只作为没有地图版本时的兼容回退值。正式物理任务必须以地图版本中的尺寸为准，并把实际目标 ticks 写入动作和回放。

### 14.2 车辆转向包络

按 230 × 160 mm 底盘计算，原地转向外接圆直径约为 280 mm。物理预检要求格内净通道至少为 320 mm，包含约 20 mm/侧的初始安全余量。小于该尺寸的地图可以保存和用于非物理测试，但物理模式预检返回 `MAP_GEOMETRY_UNSAFE`，不得开始。

P1–P4 使用无墙或宽通道标定场景；P5 才加载通过车辆包络预检的正式地图。

## 15. 网站、记录和回放

网站保持单屏控制台，新增或确认显示：

- 物理配置版本与摘要。
- 质量、重心、轮径、轮距和当前摩擦场景。
- 左右轮角度、轮速、PWM 和力矩。
- 三向 ToF 原始值、滤波值和质量标记。
- IMU 姿态与角速度。
- 估算位姿、Webots 真值和两者误差。
- 控制侧滑移估算与物理真值滑移率。
- 当前动作、动作状态和完成条件。

每次运行记录：

```text
map_version_id
param_version_id
physical_profile_id
physical_profile_digest
Webots version
random seed
controller version
telemetry / sim_truth / done / error
```

回放按统一时间轴同步视频、估算轨迹、真值轨迹、轮速、ToF、姿态、摩擦、滑移和关键事件。

## 16. 分阶段实施与验收

每个阶段严格执行：

```text
失败测试 → 实现 → 目标测试 → 完整回归 → 独立提交
```

### P1：刚体底盘

- 建立两个真实车轮关节、主动轮、被动万向轮、质量、重心、惯量和碰撞体。
- 移除正常动作中的 Supervisor 位姿写入。
- 静止 10 秒不得明显漂移、持续弹跳或倾覆。
- 画面能看到车轮旋转、底盘受力和万向轮随动。

### P2：传感器链

- 编码器来自 `PositionSensor`。
- 三向距离来自独立 `DistanceSensor`。
- 姿态、角速度和加速度来自对应 Webots 设备。
- 理想环境下 ToF 几何误差不超过 10 mm。
- 启用噪声后输出包含质量标记，固定种子下可重复。

### P3：闭环动作

- 正常摩擦下执行 250 mm 直线标定动作，终点位置误差不超过 15 mm。
- 前进完成时航向误差不超过 3°。
- 原地转向 90°，角度误差不超过 3°。
- 连续执行 10 次，成功率至少 90%。
- 完成前必须经历减速与连续稳定判断。

### P4：摩擦与故障

- 覆盖 normal、low、asymmetric 和 local_patch。
- 产生可观察的轮滑、轨迹偏移和编码器/实际位移差异。
- 验证失速、空转、障碍过近、心跳超时、动作超时、暂停、停止和急停。
- 测试程序用真值判断控制侧滑移检测是否正确，控制器本身不得读取真值。

### P5：网站全链路

- 网站选择地图、参数和物理配置后能够重置并开始。
- 实时看到物理车、传感器、姿态、估算/真值误差和滑移。
- 成绩与回放保存完整物理配置摘要。
- CPU 服务器仿真实时倍率至少 0.8。
- 浏览器画面稳定达到至少 15 FPS。
- 性能不足时先降低阴影、纹理和渲染分辨率，不降低 8 ms 控制周期。

## 17. 测试层级

### 17.1 静态合同测试

- 解析 world/PROTO，确认两个轮关节、两个电机、两个编码器、三个 ToF 和 IMU 设备存在。
- 确认正常控制代码不调用整车位姿写入。
- 确认设备名称与控制器合同完全一致。

### 17.2 无 Webots 单元测试

- 使用 fake devices 测试 PID、动作状态机、减速、稳定判断和错误码。
- 测试配置解析、范围和物理配置摘要。
- 测试 `sim_truth` 不进入控制与定位输入。
- 测试 250 mm 标定距离与不同地图格长的 ticks 换算，避免重新引入固定单格距离。

### 17.3 Webots 无渲染集成测试

- 批处理运行直行、转向、停止、急停和四类摩擦场景。
- 从 PositionSensor、DistanceSensor 和 IMU 采集证据。
- 记录真实位姿误差、角度误差、滑移和仿真实时倍率。

### 17.4 网站与回放测试

- 验证模式选择、预检、重置、开始、暂停、停止和急停。
- 验证 WebSocket 状态、页面参数和版本摘要。
- 验证回放时间轴、估算/真值轨迹和成绩可重复。

### 17.5 完整回归

现有确定性仿真、协议、Dashboard、任务状态机、地图、定位、滑移、评分、回放和 ESP32 构建测试必须继续通过。

## 18. 迁移和回滚

实施期间保留两个入口：

- 确定性无 Webots后端：用于快速测试和故障隔离。
- 新物理 Webots 世界：用于正式可视化和物理验收。

在 P5 验收通过前，不直接删除现有 `MazeSimEngine` 或覆盖稳定部署入口。物理后端通过显式配置启用；若某阶段失败，可切回确定性后端继续验证上层平台，而不会把物理仿真失败误判为网站或任务系统失败。

## 19. 完成定义

本设计完成的判定是：

1. P1–P5 全部通过并有测试与运行证据。
2. 正常动作完全由车轮物理作用产生。
3. 传感器、控制观测和真值严格隔离。
4. 四类摩擦场景和核心故障能够稳定复现。
5. 网站、成绩和回放完整记录物理配置。
6. 现有完整回归和 ESP32 构建继续通过。
7. 结果标记为“物理仿真已验证”，真实小车仍按独立硬件顺序验收。
