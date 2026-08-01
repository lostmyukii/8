# Webots 物理迷宫小车实施计划

> 对应已确认设计：
> `docs/superpowers/specs/2026-08-01-webots-physical-maze-car-design.md`

**目标：** 把当前通过 Supervisor 直接写位姿的动作级动画，升级为由左右主动轮、被动万向轮、刚体接触、编码器、三向 ToF 和 IMU 驱动的物理仿真；继续复用现有网站、单一 `DeviceSession`、一行一个 JSON 协议、任务状态机、地图版本、融合定位、评分和回放。

**插入位置：** 本计划优先于
`docs/superpowers/plans/2026-08-01-dual-mode-maze-control-platform.md`
中尚未开始的 Task 9–14。完成本计划 Task 1–12 后，再回到原计划 Task 9。

**技术路线：**

- 保留 `MazeSimEngine`，继续承担快速、确定性、无 Webots 单元测试。
- 新增 `PhysicalMazeEngine`，通过 Webots 设备读取传感器、通过轮轴电机驱动车辆。
- `SimProtocolServer` 面向引擎协议，不再依赖具体的 `MazeSimEngine` 类型。
- 物理配置使用不可变 YAML 快照、内容摘要和固定随机种子。
- Webots 真值通过独立 `TruthObserver` 产生，只进入评估、评分和回放。
- 物理模式正式任务使用地图实际格宽/格高换算 `target_ticks`。
- P5 通过前不覆盖当前稳定部署入口；新旧世界可显式切换和回滚。

## 当前基线

计划编写时在本地重新验证：

```text
Python tests: 163 passed
Python compileall: PASS
ESP32 PlatformIO build: PASS
RAM: 6.9%
Flash: 24.2%
git diff --check: PASS
本机 Webots CLI: 未安装
```

当前证据边界：

- 本地可以完成静态合同、fake device、协议、Dashboard 和 PlatformIO 回归。
- Webots 无渲染物理验收、真实帧率和 CPU 实时倍率必须在已安装 Webots
  R2025a 的服务器上执行。
- 物理仿真通过不等于真实 RDK X3、ESP32、电机、编码器、ToF 或整车已验收。

## 不可破坏的约束

1. RDK/网站不发送 8 ms 级左右轮 PWM；高频闭环只存在于模拟 ESP32 或真实 ESP32。
2. 每个动作保留 `action_id`，完成或失败必须返回同一 `action_id`。
3. 正常动作不能写整车 `translation`/`rotation`；只有原子重置可设置位姿并调用 `resetPhysics()`。
4. `sim_truth` 不得进入 `extract_fusion_telemetry()`、`PoseFusion`、`SlipEstimator` 的控制输入或 `MazePlanner`。
5. 物理配置只能在重置前选择，运行中不得静默修改质量、重心、摩擦或电机物理参数。
6. 自动调参不能修改安全参数；本计划不实现强化学习或大模型闭环。
7. 缺失核心 Webots 设备、摘要不匹配、地图净通道不足或物理数值非有限时不得启动。
8. 不删除现有 `maze_world.wbt`、`MazeSimEngine` 和稳定服务入口，直到 P5 验收完成。
9. 不触碰未跟踪的 `.vscode/` 和 `去年程序备份/`。
10. 不把服务器地址、密码、设备路径、密钥或其他敏感信息写入源码、测试、计划或日志。

## 每个 Task 的固定执行协议

每次只执行一个 Task，并按下列顺序：

1. 确认工作区只包含预期改动。
2. 先写测试并运行，保存明确的 RED 证据。
3. 只实现使当前测试通过的最小代码。
4. 运行目标测试。
5. 运行完整 Python 回归和 `compileall`。
6. 运行 ESP32 PlatformIO 构建，确认仿真改动没有破坏固件。
7. 对涉及 Webots 的 Task，在可用服务器执行对应无渲染验收。
8. 运行 `git diff --check`。
9. 搜索秘密、真实设备路径和公网凭据。
10. 独立提交，并把 RED、目标测试、完整回归和物理证据写回本计划。

通用回归命令：

```bash
.venv/bin/python -m compileall -q rdk_maze_tuner simulation
.venv/bin/python -m pytest rdk_maze_tuner/tests -q
/Users/yukii/.platformio/penv/bin/pio run -d esp32_firmware
git diff --check
```

服务器上 PlatformIO 的可执行路径可以不同，但必须记录实际命令和结果。

---

## 阶段 P0：配置与兼容边界

### Task 1：不可变物理配置和四种表面配置

**文件：**

- Create: `simulation/webots/maze_car/config/physical_profiles/normal-v1.yaml`
- Create: `simulation/webots/maze_car/config/physical_profiles/low-v1.yaml`
- Create: `simulation/webots/maze_car/config/physical_profiles/asymmetric-v1.yaml`
- Create: `simulation/webots/maze_car/config/physical_profiles/local-patch-v1.yaml`
- Create: `simulation/webots/maze_car/physical_config.py`
- Create: `rdk_maze_tuner/tests/test_physical_config.py`
- Modify: `simulation/webots/maze_car/README.md`

- [x] **1.1 写配置加载失败测试**

覆盖：

- `normal-v1` 解析出轮半径 `0.0325 m`、轮距 `0.135 m`、总质量
  `1.20 kg`、编码器 `1103 ticks/rev`。
- 控制周期必须为 `8 ms`，telemetry 周期为 `50 ms`。
- `profile_id`、规范化完整快照、SHA-256 摘要和随机种子稳定可重复。
- 四个 profile 的几何、质量和传感器配置一致，只允许表面/轮胎接触配置不同。
- 左右摩擦、局部低摩擦区域、噪声、dropout、死区和电机响应均有明确范围。
- 总质量必须等于主体质量加两轮质量；重心和惯量必须为有限数。
- 缺字段、未知字段、负质量、非法摩擦、非有限数、重复 ID 和摘要不匹配均拒绝。

Run:

```bash
.venv/bin/python -m pytest rdk_maze_tuner/tests/test_physical_config.py -q
```

Expected: RED，因为 `physical_config.py` 和 profile 文件尚不存在。

- [x] **1.2 实现强类型配置合同**

使用冻结 dataclass 表示：

```text
GeometryConfig
BodyConfig
MotorConfig
EncoderConfig
ToFConfig
ImuConfig
SurfaceConfig
RuntimeConfig
PhysicalProfile
```

`PhysicalProfileRepository` 只从受控目录按 ID 读取文件，完成：

- YAML 解析。
- 严格字段检查。
- 有限数和范围检查。
- 规范化 JSON。
- 内容摘要。
- 固定种子。
- 列出可用 profile。

配置读取不得修改文件，也不得把 YAML 中的任意路径传给文件系统。

- [x] **1.3 固化第一版参数**

共同基线：

```text
wheel_radius_m=0.0325
wheel_width_m=0.026
axle_track_m=0.135
chassis_length_m=0.230
chassis_width_m=0.160
total_mass_kg=1.20
wheel_mass_kg=0.06
center_of_mass_m=[0.0, 0.070, 0.010]
max_velocity_rad_s=20.0
max_torque_nm=0.60
response_time_s=0.08
pwm_dead_zone=0.18
ticks_per_revolution=1103
ToF range=0.03–2.0 m
ToF FoV=25 degrees
basic_time_step_ms=8
telemetry_period_ms=50
```

表面场景：

```text
normal:       left=0.90, right=0.90
low:          left=0.25, right=0.25
asymmetric:   left=0.35, right=0.90
local-patch:  base=0.90, patch=0.25
```

- [x] **1.4 运行目标测试和完整回归**

- [x] **1.5 提交检查点**

```text
feat: add versioned physical simulation profiles
```

Task 1 实施记录（2026-08-01）：

- RED：`test_physical_config.py` 在收集阶段因
  `simulation.webots.maze_car.physical_config` 不存在而失败。
- 新增 4 个完整 YAML profile；几何、质量、电机、编码器、ToF、IMU、
  runtime 和固定种子一致，只允许 `profile_id` 与 surface 场景不同。
- 配置合同使用冻结 dataclass、严格字段白名单、有限数/范围/概率校验、
  质量守恒、正定惯量、受控目录和重复 ID 检查；规范 JSON 计算 SHA-256，
  可用 `expected_digest` 拒绝摘要不匹配。
- 固定种子均为 `20260801`；摘要分别为
  `normal-v1=da0ca2fc...451f`、
  `low-v1=b021fa16...c543`、
  `asymmetric-v1=7cc8b04f...ca68`、
  `local-patch-v1=b95f1601...f098`。
- 目标测试：18 passed。
- 完整 Python 回归：181 passed；`compileall` 通过。
- ESP32 PlatformIO 构建通过：RAM 6.9%，Flash 24.2%。
- Task 1 只建立配置资产，没有启动 Webots、修改稳定 world 或声称物理
  仿真/真实小车已验收。

### Task 2：抽象仿真引擎合同并保持确定性后端不变

**文件：**

- Create: `simulation/webots/maze_car/engine_contract.py`
- Create: `rdk_maze_tuner/tests/test_sim_engine_contract.py`
- Modify: `simulation/webots/maze_car/controllers/maze_sim_controller/sim_server.py`
- Modify: `simulation/webots/maze_car/controllers/maze_sim_controller/sim_engine.py`
- Modify: `rdk_maze_tuner/tests/test_webots_sim_bridge.py`

- [x] **2.1 写引擎替换失败测试**

用最小 fake engine 验证 `SimProtocolServer` 只依赖：

```python
ready_message()
telemetry_message()
handle(message, now_ms=...)
tick(now_ms=...)
on_client_connected(...)
on_client_disconnected(...)
close()
```

断开回调用于物理引擎本地停车；确定性引擎可以提供无操作实现。

Run:

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_sim_engine_contract.py \
  rdk_maze_tuner/tests/test_webots_sim_bridge.py -q
```

Expected: RED，因为服务器仍直接标注 `MazeSimEngine` 且没有连接生命周期合同。

- [x] **2.2 实现 `SimulationProtocolEngine`**

- 用 `Protocol` 描述最小合同。
- `SimProtocolServer` 构造函数只接受该合同。
- 新客户端连接和断开时通知引擎。
- malformed JSON 仍返回 `INVALID_JSON`。
- 单客户端、loopback、换行 JSON 和发送顺序保持不变。
- 服务器关闭时只关闭自己的 socket，并调用引擎 `close()`；不操作其他服务。

- [x] **2.3 保持旧后端回归**

必须继续通过：

- ACK → telemetry → matching done。
- stop、estop、clear_estop。
- load_map/reset/start 的版本和摘要回显。
- `DeviceSession` 单 reader。
- standalone deterministic server。

- [x] **2.4 运行目标测试和完整回归**

- [x] **2.5 提交检查点**

```text
refactor: abstract the simulation protocol engine
```

Task 2 实施记录（2026-08-01）：

- RED：新测试因 `simulation.webots.maze_car.engine_contract` 不存在而在
  收集阶段失败。
- 新增 runtime-checkable `SimulationProtocolEngine`；TCP server 不再
  import 或标注具体 `MazeSimEngine`。
- server 在初始 ready/telemetry 前通知连接，在 EOF、socket 错误和关闭时
  只通知一次断开；关闭幂等，并清理自己的 client/listener 后调用引擎
  `close()`。
- 确定性后端只增加连接生命周期无操作钩子；ACK、telemetry、matching
  done、地图摘要、stop/estop、malformed JSON 和单 reader 行为保持。
- 目标测试：10 passed。
- 完整 Python 回归：186 passed；`compileall` 通过。
- ESP32 PlatformIO 构建通过：RAM 6.9%，Flash 24.2%。

---

## 阶段 P1：刚体底盘

### Task 3：建立物理 PROTO、标定世界和静态合同

**文件：**

- Create: `simulation/webots/maze_car/protos/PhysicalMazeCar.proto`
- Create: `simulation/webots/maze_car/worlds/maze_physical_calibration.wbt`
- Create: `simulation/webots/maze_car/worlds/maze_physical_world.wbt`
- Create: `rdk_maze_tuner/tests/test_webots_physical_model.py`
- Modify: `simulation/webots/maze_car/README.md`

- [x] **3.1 写 PROTO/world 静态失败测试**

解析文本合同，验证：

- `WorldInfo.basicTimeStep` 为 8，`FPS` 为 24，随机种子明确。
- 物理世界使用 `PhysicalMazeCar`，不继续内嵌旧静态轮子。
- 恰好两个主动 `HingeJoint`。
- 恰好两个 `RotationalMotor` 和两个 `PositionSensor`。
- 前、左、右三个 `DistanceSensor` 名称唯一。
- 存在 `InertialUnit`、`Gyro`、`Accelerometer`。
- 主体、两轮和万向轮有明确碰撞体与 `Physics`。
- 左右轮使用不同 `contactMaterial` 名称。
- 地面和局部低摩擦区域均有真实 `boundingObject`。
- 控制器名称为 `maze_physical_controller`，`supervisor TRUE` 只服务于重置、地图和真值观察。
- 物理控制器中禁止出现每 tick 的
  `translation.setSFVec3f` / `rotation.setSFRotation`。

Run:

```bash
.venv/bin/python -m pytest rdk_maze_tuner/tests/test_webots_physical_model.py -q
```

Expected: RED，因为 PROTO 和两个物理 world 尚不存在。

- [x] **3.2 建立简化但稳定的碰撞模型**

PROTO 对外暴露：

```text
wheelRadius
wheelWidth
axleTrack
chassisLength
chassisWidth
bodyMass
wheelMass
centerOfMass
inertiaMatrix
controller
controllerArgs
```

视觉层表现蓝色底盘、两侧 TT 轮、上层电子板、前部摄像头和万向轮。
碰撞层只使用稳定的 Box/Cylinder/Sphere，不使用复杂视觉网格。

- [x] **3.3 建立两个 world**

`maze_physical_calibration.wbt`：

- 无墙或宽通道。
- 带 250 mm 标定标记和 90° 转向标记。
- 用于 P1–P4。

`maze_physical_world.wbt`：

- 保留 `DEF MAZE_WALLS Group`。
- 用于加载正式地图。
- 包含普通地面和可选局部低摩擦碰撞区域。

- [x] **3.4 在服务器执行 P1 初验**

用 Webots 无渲染运行静止 10 秒，记录：

- 初始/结束位置差。
- 初始/结束姿态差。
- 最大垂直速度。
- 是否倾覆、穿地、持续弹跳。
- 仿真是否出现设备或物理错误。

首次目标只验证结构稳定，不调整 PID。

- [x] **3.5 运行完整回归**

- [x] **3.6 提交检查点**

```text
feat: add the physical Webots maze car model
```

P1 完成条件：

- 静止 10 秒无明显漂移、倾覆或持续弹跳。
- 两个主动轮和万向轮在画面中结构正确。
- 正常动作路径尚未接入，但物理 world 可以稳定加载。

Task 3 实施记录（2026-08-01）：

- RED：5 个静态合同测试因 PROTO、两个物理 world 和控制器尚不存在而
  全部失败。
- 新增 `PhysicalMazeCar.proto`：两个主动轮铰链、两个电机与编码器、
  前/左/右 ToF、IMU、陀螺仪、加速度计和前置被动万向轮；主体、两轮
  和万向轮均使用独立基础碰撞体与 Physics。
- 新增标定 world 和正式物理 world，明确使用 NUE（Y 轴向上）、8 ms
  仿真步长、24 FPS、固定随机种子 20260801、左右轮独立接触材质以及
  可碰撞的局部低摩擦区域。
- 服务器使用 Webots R2025a 和软件渲染完成 10.008 秒无渲染稳定性初验：
  水平漂移 0 m、垂直沉降 0.000011242 m、姿态变化 0.004738°、
  最大垂直速度 0.000299947 m/s、最大倾角 0.004738°，未倾覆、未穿地。
- 初验中先发现并修复 R2025a `inertiaMatrix` 类型、激光单光线约束以及
  默认 ENU 坐标系导致的重力方向问题；没有以位姿写入掩盖物理错误。
- 目标测试：5 passed。
- 完整 Python 回归：191 passed；`compileall` 通过。
- ESP32 PlatformIO 构建通过：RAM 6.9%，Flash 24.2%。

---

## 阶段 P2：物理设备与传感器链

### Task 4：设备适配器、传感器换算和真值隔离

**文件：**

- Create: `simulation/webots/maze_car/controllers/maze_physical_controller/__init__.py`
- Create: `simulation/webots/maze_car/controllers/maze_physical_controller/physical_types.py`
- Create: `simulation/webots/maze_car/controllers/maze_physical_controller/physical_devices.py`
- Create: `simulation/webots/maze_car/controllers/maze_physical_controller/physical_telemetry.py`
- Create: `simulation/webots/maze_car/controllers/maze_physical_controller/truth_observer.py`
- Create: `rdk_maze_tuner/tests/test_physical_devices.py`
- Create: `rdk_maze_tuner/tests/test_physical_telemetry.py`
- Modify: `rdk_maze_tuner/core/protocol.py`
- Modify: `rdk_maze_tuner/tests/test_protocol.py`

- [x] **4.1 写 fake device 失败测试**

覆盖：

- 设备名称完全匹配 PROTO。
- 缺左/右电机、编码器或任一核心 ToF 时返回
  `SIM_DEVICE_MISSING`，并将左右电机归零。
- 轮角转换为量化 ticks，方向和累计逻辑正确。
- 轮角差和 8 ms 时间差转换为轮速。
- ToF 原始米制读数转换为 mm，执行范围、滤波、噪声和 dropout 质量标记。
- 固定种子下噪声和 dropout 可重复。
- IMU、陀螺仪和加速度计非有限数返回 `SIM_PHYSICS_ERROR`。
- 电机命令受最大速度、最大力矩和有限数限制。

- [x] **4.2 写真值隔离失败测试**

覆盖：

- `PhysicalDeviceSample` 不包含 Supervisor 位姿。
- `PhysicalTelemetryProvider` 只消费设备 sample。
- `TruthObserver` 单独消费 Supervisor node。
- `extract_fusion_telemetry()` 丢弃整个 `sim_truth`。
- `extract_simulation_truth()` 只允许评估字段：
  `x_mm`、`y_mm`、`yaw_deg`、线速度、角速度、左右滑移率、
  活跃表面和碰撞次数。
- 将极端 `sim_truth` 注入 telemetry 不改变 `PoseFusion` 结果。
- 将极端 `sim_truth` 注入 telemetry 不改变 `SlipEstimator` 结果。

Run:

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_physical_devices.py \
  rdk_maze_tuner/tests/test_physical_telemetry.py \
  rdk_maze_tuner/tests/test_protocol.py \
  rdk_maze_tuner/tests/test_pose_fusion.py -q
```

Expected: RED，因为物理设备模块尚不存在。

- [x] **4.3 实现唯一 Webots 设备入口**

`PhysicalDeviceAdapter` 负责：

- 获取、启用和校验所有 Webots 设备。
- 每 8 ms 返回不可变 device sample。
- 将受限电机命令写入左右 `RotationalMotor`。
- reset 时清空编码器基准和滤波历史。
- 异常时无条件归零两个电机。

Webots API 只存在于适配器和 `TruthObserver`，控制内核不 import
`controller` 包，因此可以用 fake devices 测试。

- [x] **4.4 实现普通 telemetry**

至少输出：

```text
wheel_angle_left_rad / wheel_angle_right_rad
wheel_speed_left_rad_s / wheel_speed_right_rad_s
enc_left / enc_right
raw_front_mm / raw_left_mm / raw_right_mm
front_mm / left_mm / right_mm
imu_yaw_deg / yaw_rate_dps / accel_forward_mps2
quality_flags
controller_period_ms
friction_profile
```

普通 telemetry 暂不产生动作状态和 PID 输出，留给 Task 6 合并。

- [x] **4.5 运行目标测试和完整回归**

- [x] **4.6 提交检查点**

```text
feat: add Webots physical device telemetry
```

P2 第一部分完成条件：

- 所有设备数据来自 Webots device。
- 同一 profile/seed 的噪声输出可重现。
- 真值和普通设备数据在类型和调用关系上隔离。

Task 4 实施记录（2026-08-01）：

- RED：目标测试因物理设备、telemetry 和真值模块不存在，以及协议未提供
  严格真值 allowlist 而在收集阶段失败。
- `PhysicalDeviceAdapter` 是唯一设备入口；严格解析 10 个固定设备名，
  统一 8 ms 启用周期、编码器基准/量化、轮速、ToF 范围与 EMA 滤波、
  固定种子噪声/dropout、惯导换算和电机速度/力矩限幅。
- 任何核心设备缺失、初始化失败、读数非有限或电机命令异常都会先尝试
  将左右电机速度归零，再返回明确的 `SIM_*` 错误码。
- `PhysicalDeviceSample` 为不可变普通设备证据；`PhysicalTelemetryProvider`
  不能访问 Supervisor。`TruthObserver` 独立读取 Supervisor，协议仅允许
  9 个评估字段，`sim_truth` 整体不会进入 PoseFusion 或 SlipEstimator。
- 固定 profile/seed 的噪声与 dropout 序列已由双适配器测试证明可重复。
- 目标测试：26 passed。
- 完整 Python 回归：204 passed；`compileall` 通过。
- ESP32 PlatformIO 构建通过：RAM 6.9%，Flash 24.2%。

---

## 阶段 P3：模拟 ESP32 闭环动作

### Task 5：PID、电机响应和非阻塞动作控制内核

**文件：**

- Create: `simulation/webots/maze_car/controllers/maze_physical_controller/pid.py`
- Create: `simulation/webots/maze_car/controllers/maze_physical_controller/motor_model.py`
- Create: `simulation/webots/maze_car/controllers/maze_physical_controller/action_controller.py`
- Create: `rdk_maze_tuner/tests/test_physical_pid.py`
- Create: `rdk_maze_tuner/tests/test_physical_action_controller.py`

- [x] **5.1 写 PID 和电机模型失败测试**

覆盖：

- 8 ms 速度 PID。
- 积分限幅、输出限幅、导数对 measurement、reset 后无历史残留。
- PWM 死区、左右增益、一阶响应和最大力矩。
- 非有限输入立即失败且输出归零。
- 同样输入和固定配置结果确定。

- [x] **5.2 写动作状态机失败测试**

覆盖：

- `IDLE → MOVING_CELL/TURNING_* → SETTLING → IDLE`。
- 目标 ticks 使用动作命令值，不读取固定格长。
- 直行使用平均编码器为主、编码器差和 IMU 航向修正。
- 转向使用相反轮速，编码器目标为主、IMU 为收敛证据。
- 剩余 ticks 进入减速曲线。
- 连续多个 tick 满足位置、角度和低速阈值后才完成。
- 动作完成前不会生成 `done`。
- 任一动作均保留 `action_id`。
- 新动作不能覆盖活动动作。

- [x] **5.3 写安全与取消失败测试**

覆盖：

- 前方危险距离 → `OBSTACLE_TOO_CLOSE`。
- 动作超时 → `ACTION_TIMEOUT`。
- 心跳超时 → `HEARTBEAT_TIMEOUT`。
- 编码器停滞 → `MOTOR_STALL`。
- 编码器高速但 IMU/墙距位移不足 → `WHEELSPIN_PERSISTENT`。
- 多证据碰撞推断 → `COLLISION_SUSPECTED`。
- pause 受控减速后 → `PAUSED`，不返回成功 done。
- stop 受控减速后 → `STOPPED`。
- estop 立即归零/制动并锁定，显式 clear 后才可恢复。

Run:

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_physical_pid.py \
  rdk_maze_tuner/tests/test_physical_action_controller.py -q
```

Expected: RED，因为 PID、motor model 和 action controller 尚不存在。

- [x] **5.4 实现纯 Python 控制内核**

控制内核只消费：

- 编码器和轮速。
- 三向 ToF。
- IMU、角速度和前向加速度。
- 当前动作、参数和单调时间。

它只输出：

- 左右 PWM 等效值。
- 左右目标速度/力矩。
- 状态。
- telemetry 补充字段。
- `done` 或 `error`。

不得消费 `TruthSample`。

- [x] **5.5 运行目标测试和完整回归**

- [x] **5.6 提交检查点**

```text
feat: add the physical wheel control loop
```

Task 5 实施记录（2026-08-01）：

- RED：PID、电机模型和动作控制器测试因三个目标模块不存在而在收集阶段
  失败。
- `VelocityPid` 固定按 device sample 的 8 ms 周期工作，导数只对 measurement，
  积分和输出均限幅；非有限输入会 reset 并把最后输出归零。
- `DualMotorModel` 实现 PWM 等效死区、左右增益、一阶响应、最大速度和
  最大力矩；同一输入序列结果确定。
- `PhysicalActionController` 为非阻塞状态机，动作目标优先使用命令里的
  `target_ticks`；直行结合平均编码器、左右差和 IMU 航向，转向使用相反
  轮速并以编码器为主、IMU 为收敛证据。
- 完成必须经过减速区与连续稳定 tick；动作前不产生 done，新动作不能覆盖
  活动动作，所有 done/error 均保留 `action_id`。
- 已覆盖前障碍、动作/心跳超时、编码器停滞、持续空转、多证据碰撞、受控
  pause/stop 以及立即归零且显式 clear 才解锁的 estop。
- 目标测试：20 passed。
- 完整 Python 回归：224 passed；`compileall` 通过。
- ESP32 PlatformIO 构建通过：RAM 6.9%，Flash 24.2%。

### Task 6：PhysicalMazeEngine、协议和 Webots 主循环接线

**文件：**

- Create: `simulation/webots/maze_car/controllers/maze_physical_controller/physical_engine.py`
- Create: `simulation/webots/maze_car/controllers/maze_physical_controller/physical_world.py`
- Create: `simulation/webots/maze_car/controllers/maze_physical_controller/maze_physical_controller.py`
- Create: `rdk_maze_tuner/tests/test_physical_engine.py`
- Create: `rdk_maze_tuner/tests/test_physical_controller_contract.py`
- Modify: `simulation/webots/maze_car/map_loader.py`
- Modify: `rdk_maze_tuner/tests/test_webots_map_loader.py`

- [x] **6.1 写物理协议引擎失败测试**

用 fake device/world/truth 端到端覆盖：

- `ready` 声明物理设备和 profile 能力。
- `set_params` 只接受运动/估算参数并限幅。
- `load_profile` 验证 ID、摘要和不可变快照。
- `load_map` 保持现有版本和摘要合同。
- `reset` 原子清零动作、PID、编码器基准、滤波、estop 和评估统计。
- `start` 必须回显地图与物理 profile 摘要。
- action 立即 ACK，后续 tick 产生 telemetry，最后匹配 done/error。
- telemetry 约 20 Hz，控制 tick 仍为 8 ms。
- 客户端断开或心跳超时由引擎本地停车。
- `close()` 归零两个电机。

- [x] **6.2 写禁止 teleport 的合同测试**

物理主循环：

- 每步顺序固定为读取设备 → 控制 tick → 写电机 → 协议发送。
- 只有 `PhysicalWorldConfigurator.reset_pose()` 可以调用
  `setSFVec3f`、`setSFRotation` 和 `resetPhysics()`。
- 普通 action/tick 不持有 translation/rotation field。
- 地图重建和物理 profile 应用只在无活动动作的 reset 边界发生。

- [x] **6.3 实现物理 world 配置器**

负责：

- 应用质量、重心和惯量。
- 设置左右轮接触材料和局部地面 profile。
- 调用 `WebotsMapLoader` 重建碰撞墙。
- 按地图起点和朝向计算重置位姿。
- 检查车辆与地面/墙体没有初始穿插。
- 重置物理并等待若干 settling step。

- [x] **6.4 实现 `PhysicalMazeEngine`**

复用 Task 2 的协议合同和 Task 5 的控制内核。普通 telemetry 加入：

```text
state
pwm_left / pwm_right
motor_torque_left_nm / motor_torque_right_nm
param_version
physical_profile_id / physical_profile_digest
map_version_id / map_digest
sim_truth
```

`sim_truth` 在组装消息时由独立 `TruthObserver` 追加；控制器对象不得收到该值。

- [x] **6.5 在服务器执行 P2/P3 初验**

先运行：

- 静止传感器读数。
- 手工放置平面墙验证三向 ToF。
- 250 mm 低速动作。
- 左/右 90° 低速动作。
- stop/estop。

此 Task 可以暂时未达到最终误差阈值，但不得再通过 teleport 移动。

- [x] **6.6 运行目标测试和完整回归**

- [x] **6.7 完成 P2 物理测距验收**

在已知几何距离的平面墙前分别验证前、左、右 ToF：

- ideal/noise-off 配置的绝对误差均不超过 10 mm。
- noise-on 配置带质量标记。
- 同一 seed 重复运行时原始序列一致。
- dropout 不得变成无标记的有效近距离值。

同时用 desktop/stream 画面确认两侧车轮可见旋转、万向轮随动，
并保存与结构化传感器证据同一 run 的画面。

- [x] **6.8 提交检查点**

```text
feat: run maze actions through Webots physics
```

Task 6 实施记录（2026-08-01）：

- RED：物理引擎、world 配置器和主循环合同测试最初因目标模块不存在、
  protocol server 仍未接入真实 Webots device 而失败。
- `PhysicalMazeEngine` 现在执行一行一个 JSON 的 reset/start/action/
  stop/estop 协议，动作立即 ACK，后续由 8 ms 物理 tick 产生匹配
  `action_id` 的 done/error；telemetry 约 20 Hz。
- 普通动作只写左右轮电机，只有 `PhysicalWorldConfigurator.reset_pose()`
  持有 translation/rotation field 并调用 `resetPhysics()`；真值观察器只在
  telemetry 组装阶段追加 `sim_truth`。
- 服务器 P2 ideal/noise-off 三向 ToF 实测为
  front=536 mm、left=574 mm、right=574 mm，与已知几何值误差均为
  0 mm；noise-on 带 `tof_noise_enabled`、`tof_dropout_enabled` 和实际
  dropout 方向标记。
- 固定 seed 两次 reset 后的 16 帧原始序列完全一致，SHA-256 均为
  `4b511dba...6b3f`；dropout 没有伪装成无标记的近距离有效值。
- 服务器 P3 初验由真实轮地接触产生：250 mm 动作返回 done（1416 ms，
  左/右编码器 1336/1343）；左右 90 度动作均返回 done，初始角误差分别
  5.72 度和 4.18 度，留待 Task 8 标定到最终阈值。
- stop 返回同一 action 的 `STOPPED`，estop 返回同一 action 的 `ESTOP`，
  且只有显式 `clear_estop` 后重新解锁。
- 修正 Webots NUE 视点后，W3D 浏览器流已真实显示小车、三面碰撞墙、
  标定线、两侧驱动轮与被动万向轮；同一动作 run 的前/中/后画面保存在
  `.local/acceptance/p2-physical-{before,motion-1,after}.png`。轮上红色
  高对比标记与万向轮青色标记随各自刚体运动，画面变化与编码器证据一致。
- 目标测试：45 passed。
- 完整 Python 回归：242 passed；`compileall` 通过。
- ESP32 PlatformIO 构建通过：RAM 6.9%，Flash 24.2%。

---

## 阶段 P4：地图尺度、摩擦和故障复现

### Task 7：地图几何预检和方向相关动作距离

**文件：**

- Create: `rdk_maze_tuner/core/motion_targets.py`
- Create: `rdk_maze_tuner/tests/test_motion_targets.py`
- Create: `simulation/webots/maze_car/physical_preflight.py`
- Create: `rdk_maze_tuner/tests/test_physical_preflight.py`
- Modify: `rdk_maze_tuner/core/maze_runner.py`
- Modify: `rdk_maze_tuner/tests/test_maze_runner.py`
- Modify: `simulation/webots/maze_car/map_loader.py`
- Modify: `rdk_maze_tuner/tests/test_webots_map_loader.py`

- [x] **7.1 写动作距离换算失败测试**

覆盖：

- 250 mm、5.4 ticks/mm → 1350 ticks。
- 450 mm → 2430 ticks。
- N/S 使用 `cell_height_mm`。
- E/W 使用 `cell_width_mm`。
- 转弯继续使用明确的 90°/180° 标定 ticks。
- 地图版本缺尺寸时才回退 `robot.cell_size_cm`。
- `planned_action` 和回放事件保存实际距离、ticks/mm 和最终 `target_ticks`。
- 非正距离、非正 ticks/mm 和整数溢出拒绝。

- [x] **7.2 写车辆包络失败测试**

覆盖：

- 230 × 160 mm 底盘的外接圆直径约 280 mm。
- 物理模式净通道要求至少 320 mm。
- 净通道考虑墙厚，不能只检查 cell_width/cell_height。
- 不合格地图返回 `MAP_GEOMETRY_UNSAFE`。
- 不合格地图仍可保存并用于确定性测试，但不得开始物理任务。

Run:

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_motion_targets.py \
  rdk_maze_tuner/tests/test_physical_preflight.py \
  rdk_maze_tuner/tests/test_maze_runner.py \
  rdk_maze_tuner/tests/test_webots_map_loader.py -q
```

Expected: RED，因为 runner 仍固定读取 `motion.cell_ticks`。

- [x] **7.3 实现 `MotionTargetResolver`**

将距离换算注入 `MazeRunner`，默认实现保持旧测试兼容：

- 确定性/无地图 runner 仍可使用静态参数。
- 正式地图 runner 根据 `MazeMap.cell_width_mm`、
  `cell_height_mm` 和当前动作方向换算。
- Resolver 不访问串口，不修改参数，只返回可记录的目标描述。

- [x] **7.4 实现物理预检**

预检报告至少包含：

```text
ok
code
turning_envelope_mm
minimum_required_passage_mm
actual_passage_x_mm
actual_passage_y_mm
map_version_id
map_digest
```

- [x] **7.5 运行目标测试和完整回归**

- [x] **7.6 提交检查点**

```text
feat: derive physical actions from maze geometry
```

Task 7 实施记录（2026-08-01）：

- RED：`test_motion_targets.py` 和 `test_physical_preflight.py` 在收集阶段
  分别因 `motion_targets`、`physical_preflight` 模块不存在而失败。
- `MotionTargetResolver` 为纯换算组件：250 mm × 5.4 ticks/mm =
  1350 ticks，450 mm = 2430 ticks；N/S 使用 `cell_height_mm`，E/W
  使用 `cell_width_mm`，只有正式地图缺失对应尺寸时才回退
  `robot.cell_size_cm`。
- `MazeRunner` 的 `planned_action`、事件流和 JSONL 回放输入现在同时记录
  `direction`、`distance_mm`、`ticks_per_mm`、`target_ticks`、
  `target_source` 和转向角；默认 resolver 每步从当前参数重新建立，保留
  自动调参后下一动作生效的旧语义。
- 距离、ticks/mm、转向 ticks、结果 ticks 只接受正有限值，最终
  `target_ticks` 超出 signed 32-bit 范围时拒绝。
- 230 × 160 mm 底盘转向包络实算为 280.1785 mm；物理净通道最低要求
  320 mm，X/Y 净通道均按格尺寸减墙厚计算。
- 服务器协议实测：450 mm/40 mm 地图可 start；300 mm/20 mm 地图可
  load/reset 和用于确定性编译，但 physical start 返回
  `MAP_GEOMETRY_UNSAFE`，报告净通道 X/Y 均为 280 mm，并携带地图版本、
  摘要和包络值。
- 目标测试：48 passed。
- 完整 Python 回归：269 passed；`compileall` 通过。
- ESP32 PlatformIO 构建通过：RAM 6.9%，Flash 24.2%。

### Task 8：四种摩擦场景、真值滑移和故障注入

**文件：**

- Create: `simulation/webots/maze_car/physical_scenarios.py`
- Create: `simulation/webots/maze_car/config/acceptance_scenarios.yaml`
- Create: `rdk_maze_tuner/tests/test_physical_scenarios.py`
- Create: `rdk_maze_tuner/tests/test_physical_faults.py`
- Modify: `simulation/webots/maze_car/controllers/maze_physical_controller/truth_observer.py`
- Modify: `simulation/webots/maze_car/controllers/maze_physical_controller/physical_engine.py`
- Modify: `rdk_maze_tuner/core/protocol.py`
- Modify: `rdk_maze_tuner/tests/test_protocol.py`

- [x] **8.1 写场景合同失败测试**

每个 scenario 必须冻结：

```text
scenario_id
physical_profile_id
map/world
seed
action sequence
expected observations
acceptance thresholds
timeout
```

覆盖 normal、low、asymmetric、local_patch，不允许运行时随机替换 profile。

- [x] **8.2 写真值滑移失败测试**

验证：

```text
wheel_surface_speed = wheel_angular_speed * wheel_radius
slip_rate = (wheel_surface_speed - body_longitudinal_speed) /
            max(abs(wheel_surface_speed), epsilon)
```

- 正常场景低滑移。
- low 场景双轮滑移升高。
- asymmetric 场景左右滑移和航向变化不同。
- local_patch 只有进入指定区域后出现瞬态。
- 控制器源码和调用参数均不包含 TruthSample。

- [x] **8.3 写故障注入失败测试**

覆盖：

- 缺设备。
- 非法 profile。
- 非有限传感器/物理值。
- 前方障碍。
- 心跳超时。
- 动作超时。
- 电机失速。
- 持续空转。
- 疑似碰撞。
- pause、stop、estop 和 clear_estop。

Run:

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_physical_scenarios.py \
  rdk_maze_tuner/tests/test_physical_faults.py \
  rdk_maze_tuner/tests/test_protocol.py -q
```

Expected: RED，因为场景、完整真值滑移和故障矩阵尚不存在。

- [x] **8.4 实现场景和评估**

- 场景选择只发生在 reset 前。
- `TruthObserver` 记录真实轨迹、刚体速度、滑移和碰撞次数。
- 控制侧继续只从编码器、ToF 和 IMU 推断。
- 场景运行结果输出结构化指标，不以截图代替数值证据。

- [x] **8.5 在服务器执行 P3/P4 调参与验收**

P3 最终阈值：

```text
250 mm 直线终点误差 <= 15 mm
直线航向误差 <= 3 degrees
90 degrees 转向误差 <= 3 degrees
连续 10 次成功率 >= 90%
```

P4 要求四个 profile 的轨迹、滑移、编码器/实际位移差异可观察且固定种子可重现。

调参只改物理 profile 或模拟 ESP32 运动参数的新版本；每轮保存旧值、新值、原因和结果。不得为了通过测试读取 truth 修正控制。

- [x] **8.6 运行完整回归**

- [x] **8.7 提交检查点**

```text
feat: add repeatable physical friction scenarios
```

P3/P4 完成条件：

- 正常动作达到设计误差和成功率。
- 四类摩擦能产生不同的真实轨迹证据。
- 核心故障稳定产生规定错误码。
- 控制路径没有真值依赖。

实施记录（2026-08-01）：

- RED 阶段确认场景仓库、结构化滑移指标与完整故障矩阵尚不存在；
  实现后 Task 8 目标回归 `28 passed`。
- 四个场景均绑定不可变 profile 摘要和固定种子 `20260801`；传感器噪声/
  丢包序列在 reset 后由同一种子复现。Webots 两次 250 mm 物理复跑终点
  差异为 5.94 mm、航向差 0.43°，处于 P3 误差预算内。
- P3 连续实测：直线 10/10 成功，最大距离误差 3.264 mm、最大航向误差
  1.763°；左转 10/10 成功，最大角度误差 1.857°；右转 10/10 成功，
  最大角度误差 2.075°。
- P4 三次轨迹对比：normal/low/asymmetric 的平均实际位移分别为
  209.590/158.187/195.758 mm，编码器-真值差分别为
  38.172/89.822/52.096 mm，平均绝对航向分别为
  3.193°/4.513°/7.457°，差异可观测。
- local-patch 路径 8/8 动作完成，检测到 2 次地面切换、52 个低摩擦
  telemetry 帧且无碰撞；运动帧平均滑移从普通地面的 0.926 升至
  低摩擦区的 2.333。统计器会剔除静止帧，防止零轮速放大滑移率。
- 为使 65 mm 车轮和 230 × 160 mm 底盘在 P3 控制范围内，profile
  `max_torque_nm` 从 0.18 调整为 0.60；直线可用力矩仍限制为
  0.18 Nm，转向可用完整力矩，并增加预测制动、目标越界锁存和主动保持。
- 完整 Python 回归 `299 passed`，`compileall` 和 `git diff --check`
  通过；ESP32 PlatformIO 构建通过（RAM 6.9%，Flash 24.2%）。

---

## 阶段 P5：网站、版本、成绩与部署

### Task 9：物理 profile 资产、任务选择和 run 固化

**文件：**

- Create: `rdk_maze_tuner/platform/migrations/003_physical_profiles.sql`
- Create: `rdk_maze_tuner/platform/physical_profile_repository.py`
- Create: `rdk_maze_tuner/dashboard/routes/physical_profiles.py`
- Create: `rdk_maze_tuner/tests/test_physical_profile_repository.py`
- Modify: `rdk_maze_tuner/platform/database.py`
- Modify: `rdk_maze_tuner/platform/modes/base.py`
- Modify: `rdk_maze_tuner/platform/modes/simulation.py`
- Modify: `rdk_maze_tuner/platform/modes/real.py`
- Modify: `rdk_maze_tuner/platform/task_orchestrator.py`
- Modify: `rdk_maze_tuner/dashboard/routes/tasks.py`
- Modify: `rdk_maze_tuner/dashboard/app.py`
- Modify: `rdk_maze_tuner/tests/test_platform_database.py`
- Modify: `rdk_maze_tuner/tests/test_mode_adapters.py`
- Modify: `rdk_maze_tuner/tests/test_task_orchestrator.py`
- Modify: `rdk_maze_tuner/tests/test_dashboard.py`

- [x] **9.1 写数据库和 repository 失败测试**

迁移新增：

```text
physical_profiles
runs.physical_profile_id
runs.physical_profile_digest
runs.random_seed
runs.controller_version
runs.webots_version
```

要求：

- 同一 ID 和摘要幂等导入。
- 同一 ID 不得被不同内容静默覆盖。
- run 引用确切 profile 和摘要。
- run 开始后 profile 不可变。
- profile 快照可从数据库恢复，不依赖未来被修改的 YAML。

- [x] **9.2 写模式适配器失败测试**

仿真任务流程：

```text
create task with physical_profile_id
→ context-aware preflight
→ load_profile
→ load_map
→ reset
→ start
```

每个 ACK 必须回显 profile ID/digest 和 map ID/digest。任何不匹配返回
`PHYSICAL_PROFILE_ACK_MISMATCH` 或原有 `MAP_ACK_MISMATCH`。

实车模式不得加载 Webots profile；若请求非空仿真 profile，必须明确拒绝或记录为不适用，不能悄悄当作实车参数。

- [x] **9.3 写任务预检失败测试**

- 默认仿真 profile 为 `normal-v1`。
- `preflight()` 收到任务的 map/param/profile 上下文。
- 物理地图包络不合格时任务停在 `PREFLIGHT` 并返回
  `MAP_GEOMETRY_UNSAFE`。
- reset 前创建 run，并写入完整 profile 摘要和种子。
- snapshot、事件和任务 API 均包含 profile 字段。

Run:

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_physical_profile_repository.py \
  rdk_maze_tuner/tests/test_platform_database.py \
  rdk_maze_tuner/tests/test_mode_adapters.py \
  rdk_maze_tuner/tests/test_task_orchestrator.py \
  rdk_maze_tuner/tests/test_dashboard.py -q
```

Expected: RED，因为平台尚无物理 profile 资产。

- [x] **9.4 实现只读 profile API**

```text
GET /api/physical-profiles
GET /api/physical-profiles/{id}
```

第一版不提供网页任意编辑质量和摩擦；新 profile 必须通过受审查 YAML
和独立提交加入，防止运行中静默改物理条件。

- [x] **9.5 扩展 task/run 合同**

Task 和 run 记录：

```text
physical_profile_id
physical_profile_digest
physical_profile_snapshot
random_seed
controller_version
Webots version
```

`SimulationModeAdapter` 使用 profile provider 验证并下发，`TaskOrchestrator`
只保存已验证快照，不直接读取 Webots。

平台 repository 复用 Task 1 的解析、校验和摘要实现，只增加 SQLite
持久化和查询，不再实现第二套 YAML 规则。

- [x] **9.6 运行目标测试和完整回归**

- [x] **9.7 提交检查点**

```text
feat: version physical simulation runs
```

实施记录（2026-08-01）：

- RED 阶段首先出现 `physical_profile_repository` 缺失的 collection
  error；随后分别验证了不可变数据库、profile/map ACK、物理地图预检和
  run 快照合同。
- 迁移 003 新增 `physical_profiles` 和六个 run 身份字段；SQLite trigger
  阻止 profile 更新/删除及 run 物理身份变更。同一 ID/摘要可幂等导入，
  同一 ID 的不同内容明确冲突。
- `SimulationModeAdapter` 按 `load_profile → load_map → reset → start`
  顺序下发并验证双摘要；确定性旧后端明确返回
  `PHYSICAL_BACKEND_REQUIRED`，实车模式明确返回
  `PHYSICAL_PROFILE_NOT_APPLICABLE`。
- map/profile 上下文在连接预检阶段计算 230 × 160 mm 底盘包络；不安全
  地图保持在 `PREFLIGHT`、返回 `MAP_GEOMETRY_UNSAFE`，且不创建 run。
- 仿真任务默认 `normal-v1`；任务、事件 metadata、SQLite run 和 run API
  都暴露 profile ID/摘要/快照/种子及 controller/Webots 版本。
- 只读 API `GET /api/physical-profiles` 与
  `GET /api/physical-profiles/{id}` 已接入登录认证。
- Task 9 目标回归 `43 passed`；完整 Python 回归 `308 passed`，
  `compileall` 与 `git diff --check` 通过。

### Task 10：控制台物理参数、实时证据、成绩和回放

**文件：**

- Modify: `rdk_maze_tuner/dashboard/templates/index.html`
- Modify: `rdk_maze_tuner/dashboard/static/api.js`
- Modify: `rdk_maze_tuner/dashboard/static/state.js`
- Modify: `rdk_maze_tuner/dashboard/static/render.js`
- Modify: `rdk_maze_tuner/dashboard/static/app.js`
- Modify: `rdk_maze_tuner/dashboard/static/app.css`
- Modify: `rdk_maze_tuner/dashboard/static/replay.js`
- Modify: `rdk_maze_tuner/dashboard/state.py`
- Modify: `rdk_maze_tuner/platform/replay.py`
- Modify: `rdk_maze_tuner/platform/scoring.py`
- Modify: `rdk_maze_tuner/tests/test_dashboard_ui.py`
- Modify: `rdk_maze_tuner/tests/test_replay.py`
- Modify: `rdk_maze_tuner/tests/test_scoring.py`

- [x] **10.1 写控制台显示失败测试**

Webots 实时画面下方必须显示：

- profile ID、摘要、随机种子和 Webots 版本。
- 质量、重心、轮径、轮距和摩擦场景。
- 车头方向、IMU 航向和置信度。
- 左右轮角度、轮速、PWM、力矩和编码器。
- 三向 ToF 原始值、滤波值和质量标记。
- 控制侧滑移估算、物理真值滑移和两者差异。
- 估算位姿、Webots 真值和位置/角度误差。
- 当前动作、完成条件、安全状态和最近错误。
- 始终可见的急停按钮。

仿真真值必须明确标记“仅评估”，实车模式不得伪造该卡片。

- [x] **10.2 写回放和评分失败测试**

回放清单新增同步通道：

```text
physical_profile
wheel
tof
imu
control
slip_estimate
sim_truth
surface
fault
```

原始指标新增：

- 直线距离误差。
- 转向误差。
- 航向漂移。
- 左右滑移率。
- 碰撞次数。
- stall/wheelspin/safety fault 次数。
- 控制周期和仿真实时倍率。

缺失证据继续明确为缺失，不生成伪分数。更换评分 profile 只重算综合分，不改原始指标。

- [x] **10.3 实现 profile 选择和运行锁定**

- 新任务创建前可选择四种 profile。
- reset 后选择器锁定，只有回到可重置状态才能更换。
- 页面明确显示当前 run 使用的 profile，而不是全局默认值。
- profile 切换需要控制权租约；急停仍允许两名已登录用户操作。

- [x] **10.4 实现物理证据卡片和回放轨**

保持单屏控制中心，不开发独立 3D 桥接页面。复用 Webots 流作为画面，
Dashboard 只显示结构化遥测、图表、任务、成绩和回放。

- [x] **10.5 浏览器验收**

使用独立测试数据检查：

- 1440×1000。
- 1280×800。
- 768×1000。
- 物理卡片不遮挡 Webots 画面、任务按钮或急停。
- 实车/仿真标识不会混淆。
- 估值与真值标签在小屏仍清晰。

- [x] **10.6 运行 JavaScript、目标测试和完整回归**

```bash
for file in rdk_maze_tuner/dashboard/static/*.js; do
  node --check "$file"
done
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_dashboard_ui.py \
  rdk_maze_tuner/tests/test_replay.py \
  rdk_maze_tuner/tests/test_scoring.py -q
```

- [x] **10.7 提交检查点**

```text
feat: expose physical simulation evidence
```

### Task 11：自动化 Webots 物理验收和服务器安全切换

**文件：**

- Create: `simulation/webots/maze_car/tools/__init__.py`
- Create: `simulation/webots/maze_car/tools/run_physical_acceptance.py`
- Create: `simulation/webots/maze_car/tools/physical_acceptance_schema.py`
- Create: `rdk_maze_tuner/tests/test_physical_acceptance_runner.py`
- Modify: `deploy/server/systemd/maze-webots-stream.service`
- Modify: `deploy/server/systemd/maze-webots-desktop.service`
- Modify: `deploy/server/systemd/maze-webots-headless.service`
- Modify: `deploy/server/deploy_release.sh`
- Modify: `deploy/server/rollback_release.sh`
- Modify: `deploy/server/maze-sim-mode`
- Modify: `simulation/webots/maze_car/README.md`

- [x] **11.1 写验收 runner 失败测试**

runner 必须：

- 只终止自己启动并已记录 PID 的 Webots 进程。
- 使用临时端口和临时结果目录。
- 等待 ready，有总超时。
- 通过现有 `DeviceSession` 执行场景。
- 保存原始 JSONL、profile/map 摘要和聚合报告。
- Webots 未安装时明确返回 unavailable，不伪造 PASS。
- 子进程失败、超时、协议断开和报告不完整均返回非零。

- [x] **11.2 实现 P1–P4 自动化报告**

服务器命令：

```bash
python3 -m simulation.webots.maze_car.tools.run_physical_acceptance \
  --webots /usr/local/bin/webots \
  --world simulation/webots/maze_car/worlds/maze_physical_calibration.wbt \
  --scenarios simulation/webots/maze_car/config/acceptance_scenarios.yaml \
  --output /srv/maze/shared/acceptance/physical
```

报告包含：

```text
source commit
Webots version
profile ID/digest
map ID/digest
seed
per-scenario metrics
threshold decisions
realTimeFactor
controller period
errors
overall PASS/FAIL
```

- [ ] **11.3 新 release 灰度部署**

顺序：

1. 构建只读 release 目录。
2. 在 release 目录运行 Python/PlatformIO/静态测试。
3. 停止且只停止当前仿真 service。
4. 启动物理 headless 模式执行自动验收。
5. P1–P4 全绿后切到物理 stream 模式。
6. 验证 Dashboard 到 `127.0.0.1:8765`。
7. 验证 1234/6080/5901/8765 仍只在本机或私网监听。
8. 验证网站公网只经已有 HTTPS 入口。
9. 失败时使用明确 release ID 回滚，恢复旧 `maze_world.wbt` 服务。

- [x] **11.4 修改 systemd 入口**

P1–P4 通过后，三个 Webots service 显式使用：

```text
maze_physical_world.wbt
MAZE_PHYSICAL_PROFILE_DIR
MAZE_DEFAULT_PHYSICAL_PROFILE=normal-v1
```

保持：

- stream/desktop/headless 互斥。
- TCP 只监听 `127.0.0.1:8765`。
- stream 仍使用 W3D。
- desktop 仍复用共享 VNC。
- headless 不渲染但不降低 8 ms 物理控制周期。

- [x] **11.5 P5 性能与画面验收**

记录真实值：

```text
CPU server model/vCPU/RAM
Webots realTimeFactor >= 0.8
browser visible FPS >= 15
telemetry rate about 20 Hz
controller period 8 ms
Dashboard/Webots latency
CPU/RAM usage
```

若不达标，依次降低阴影、纹理、抗锯齿和输出分辨率；禁止用降低控制频率换帧率。

- [x] **11.6 运行目标测试和完整回归**

- [x] **11.7 提交检查点**

```text
deploy: switch Webots services to physical simulation
```

Task 11 实施记录（2026-08-01）：

- 自动 runner 使用临时端口、记录 PID、总超时、DeviceSession 和完整
  schema；缺 Webots、协议断开、子进程失败或字段缺失均返回非零。
- 服务器报告 `physical-20260801T184931Z-6b28a70a` 为 PASS：
  P1 水平漂移 0 m，P2 三向 ToF 最大误差/重复离散均 0 mm，
  normal 30/30 动作成功，四种摩擦差异全部通过。
- P5 实测 RTF 0.952、可见更新 19.99 FPS、telemetry 17.23 Hz、
  8 ms 控制周期；Webots 约占整机 10.25% CPU、240.39 MiB。
- stream → desktop → stream 已验证，Dashboard 均能重连协议。
- Python 完整回归在入口修复后为 325 passed；服务器 PlatformIO 为
  RAM 6.9%、Flash 24.2%。
- 公网 80/443 当前仍在腾讯云边界超时，主机 UFW 入站计数为 0；
  因此 11.3 的“公网 HTTPS”一项保持未完成，不能写成部署全绿。

### Task 12：完整验收证据、文档和移交

**文件：**

- Create: `docs/acceptance/webots-physical-maze-car-checklist.md`
- Create: `docs/operations/webots-physical-simulation-runbook.md`
- Modify: `DEVELOPMENT.md`
- Modify: `simulation/webots/maze_car/README.md`
- Modify: `docs/superpowers/plans/2026-08-01-webots-physical-maze-car.md`

- [x] **12.1 完成 P1–P5 证据清单**

记录而不是概述：

- 每个 Task 的 RED 输出。
- 目标测试和完整回归数量。
- PlatformIO RAM/Flash。
- 每个独立 commit。
- P1 静止稳定指标。
- P2 ToF 理想误差和固定种子重复性。
- P3 直线/转向误差和 10 次成功率。
- P4 四种摩擦及故障矩阵。
- P5 realTimeFactor、浏览器 FPS、遥测率和页面视口截图。
- 当前服务器 release ID、服务状态和回滚目标。

- [x] **12.2 完整本地回归**

```bash
.venv/bin/python -m compileall -q rdk_maze_tuner simulation
.venv/bin/python -m pytest rdk_maze_tuner/tests -q
/Users/yukii/.platformio/penv/bin/pio run -d esp32_firmware
for file in rdk_maze_tuner/dashboard/static/*.js; do
  node --check "$file"
done
git diff --check
```

- [ ] **12.3 完整服务器回归**

在当前 release 目录重新执行：

- Python tests。
- compileall。
- physical acceptance runner。
- stream → desktop → stream 模式切换。
- Dashboard 登录、控制权、任务、急停、成绩和回放。
- 本地端口与公网入口检查。
- systemd 重启后历史 run 恢复。

- [x] **12.4 明确验收边界**

最终状态必须分别报告：

```text
源码/静态合同：PASS/FAIL
无 Webots 单元测试：PASS/FAIL
Webots 物理仿真：PASS/FAIL
网站全链路：PASS/FAIL
服务器性能：PASS/FAIL
ESP32 构建：PASS/FAIL
真实小车：NOT TESTED / PASS / FAIL
```

不能把 Webots PASS 写成“真车可用”。

- [x] **12.5 文档移交**

Runbook 包含：

- 选择 physical/deterministic 后端。
- 选择四种 profile。
- 启动、暂停、停止、急停和 clear_estop。
- 运行 P1–P5 验收。
- 查看原始 JSONL、成绩和回放。
- 性能调优顺序。
- 服务切换和精确回滚。
- 何时可以继续原双模式计划 Task 9。

- [ ] **12.6 最终提交**

```text
test: complete physical Webots acceptance
```

---

## 推荐提交顺序

```text
1.  feat: add versioned physical simulation profiles
2.  refactor: abstract the simulation protocol engine
3.  feat: add the physical Webots maze car model
4.  feat: add Webots physical device telemetry
5.  feat: add the physical wheel control loop
6.  feat: run maze actions through Webots physics
7.  feat: derive physical actions from maze geometry
8.  feat: add repeatable physical friction scenarios
9.  feat: version physical simulation runs
10. feat: expose physical simulation evidence
11. deploy: switch Webots services to physical simulation
12. test: complete physical Webots acceptance
```

## 完成定义

只有同时满足以下条件，本计划才算完成：

1. P1–P5 均有可复现的测试和运行证据。
2. 正常动作完全由车轮与地面的物理作用产生。
3. 两个电机、两个编码器、三个 ToF 和 IMU 均来自真实 Webots 设备。
4. 普通控制观测与 Supervisor 真值在代码、协议白名单和测试上隔离。
5. 250 mm 标定和实际地图格长换算均正确，物理模式不再固定使用 1350 ticks。
6. 车辆包络不合格的地图不能启动物理任务。
7. normal、low、asymmetric、local_patch 四类场景可重复并产生不同轨迹。
8. stop、pause、estop、断联、超时、障碍、失速和空转行为符合错误合同。
9. 网站显示物理配置、车头方向、轮速、力矩、ToF、IMU、滑移和估值/真值误差。
10. run、成绩和回放冻结地图、参数、profile、摘要、种子和版本。
11. CPU 服务器 `realTimeFactor >= 0.8`，浏览器画面 `>= 15 FPS`，控制周期仍为 8 ms。
12. Python、JavaScript、部署、故障注入和 ESP32 PlatformIO 回归全部通过。
13. 稳定部署可精确回滚到旧确定性 `maze_world.wbt`。
14. 最终交付明确标记“Webots 物理仿真已验证”，真实小车仍需独立硬件验收。
