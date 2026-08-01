# 双模式迷宫小车控制平台实施计划

> 对应设计：
> `docs/superpowers/specs/2026-08-01-dual-mode-maze-control-platform-design.md`

**目标：** 在现有 FastAPI Dashboard、Webots 仿真、RDK Python 核心和 ESP32 非阻塞固件之上，分阶段交付支持仿真/实车切换、一键自动探索、实时画面、手绘迷宫、融合定位、参数版本、成绩回放、模型建议和仿真优化的安全网站。

**架构：** FastAPI 服务器负责账户、控制权、任务编排、版本资产、模型建议、计分和回放；仿真模式的定位/规划在服务器仿真进程运行，实车模式的定位/规划和小幅规则补偿在 RDK X3 本地运行；ESP32 继续独立负责毫秒级运动闭环和最终停车。

**技术栈：**

- Python 3.12、FastAPI、Uvicorn、WebSocket、stdlib `sqlite3`
- Argon2 密码哈希、服务端会话、CSRF
- 原生 HTML/CSS/JavaScript，不引入前端构建链
- Webots R2025a、Xvfb、ffmpeg
- RDK Agent：Python、pyserial、WSS
- ESP32：PlatformIO、Arduino、ArduinoJson、VL53L0X、可选 I²C IMU
- 公网入口：Caddy + `https://8.ilelezhan.cn`
- 可选优化依赖：Optuna；后续 Gymnasium 环境

**安全铁律：**

- 不关闭 SSH 密码登录，除非用户后续明确授权。
- 不把 8000、1234、6080、5901、8765 或串口暴露公网。
- 不把模型放入电机闭环，不允许模型或自动调参修改安全参数。
- 不用仿真结果冒充真实硬件验收。
- 不在没有新鲜设备确认时烧录 ESP32。
- 每个里程碑先写失败测试，再实现，再运行完整回归。

## 当前基线

计划开始时已验证：

```text
Python tests: 57 passed
Python compileall: PASS
ESP32 PlatformIO build: PASS
Server mode: Webots stream + Dashboard
SSH alias: maze-cvm
SSH password authentication: retained
```

当前主要技术债务：

- `dashboard/state.py` 同时承担设备、地图、参数和日志状态。
- `SerialDashboardRuntime` 与 `SerialClient.execute_action()` 都可能读取同一 transport。
- 参数版本只存在内存，重启即丢失。
- 当前 `AutoTuner` 会建议 `tof.front_stop_mm`，不符合新安全域。
- Webots 世界和内部墙体为硬编码。
- 没有账户、控制权租约、任务状态机、SQLite、视频或回放。

---

## 阶段 A：网站与仿真最小闭环

### Task 1：配置、SQLite 与不可变事件基础

**文件：**

- Create: `rdk_maze_tuner/platform/__init__.py`
- Create: `rdk_maze_tuner/platform/config.py`
- Create: `rdk_maze_tuner/platform/database.py`
- Create: `rdk_maze_tuner/platform/event_store.py`
- Create: `rdk_maze_tuner/platform/migrations/001_initial.sql`
- Create: `rdk_maze_tuner/tests/test_platform_database.py`
- Create: `rdk_maze_tuner/tests/test_event_store.py`
- Modify: `.gitignore`
- Modify: `requirements.txt`

- [x] **1.1 写 SQLite 迁移失败测试**

验证新数据库创建以下表并可重复迁移：

- `users`
- `sessions`
- `control_lease`
- `devices`
- `maps`
- `map_versions`
- `param_versions`
- `runs`
- `events`
- `scores`
- `artifacts`
- `advisor_candidates`

Run:

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_platform_database.py \
  rdk_maze_tuner/tests/test_event_store.py -q
```

Expected: RED，因为 platform 存储模块尚不存在。

- [x] **1.2 实现配置和数据库生命周期**

要求：

- 数据根目录由 `MAZE_DATA_DIR` 指定，默认开发目录为 `.local/maze-data`。
- 服务器默认目录为 `/srv/maze/shared`。
- SQLite 开启 foreign keys、WAL 和 busy timeout。
- 迁移使用显式版本表，不依赖 ORM。
- 测试数据库使用 `tmp_path`，不触碰真实数据。

- [x] **1.3 实现双写事件存储**

每个 run 同时写：

- SQLite 可检索事件索引。
- `runs/<run_id>/events.jsonl` 原始事件。

JSONL 每条记录包含 `event_id`、`run_id`、单调时间、UTC 时间、类型、来源、payload 和 schema version。重复 `event_id` 必须幂等。

- [x] **1.4 运行存储测试和回归**

```bash
.venv/bin/python -m pytest rdk_maze_tuner/tests/test_platform_database.py rdk_maze_tuner/tests/test_event_store.py -q
.venv/bin/python -m pytest rdk_maze_tuner/tests -q
```

Expected: PASS。

- [x] **1.5 提交检查点**

Commit message:

```text
feat: add platform storage foundation
```

实施证据（2026-08-01）：

- RED：两份新测试因 `rdk_maze_tuner.platform` 不存在而在收集阶段失败。
- 并发 RED：两个存储实例同时重试同一 `event_id` 时稳定复现两行 JSONL，修复后只保留一行。
- 目标测试：10 passed。
- 完整 Python 回归：67 passed；`compileall` 通过。
- ESP32 PlatformIO 构建：通过，RAM 6.9%，Flash 24.1%。
- 本任务仅使用 stdlib `sqlite3` 和 POSIX `fcntl`，未新增第三方依赖，因此 `requirements.txt` 无需改动。

### Task 2：网站账户、服务端会话与控制权租约

**文件：**

- Create: `rdk_maze_tuner/platform/auth.py`
- Create: `rdk_maze_tuner/platform/control_lease.py`
- Create: `rdk_maze_tuner/dashboard/routes/__init__.py`
- Create: `rdk_maze_tuner/dashboard/routes/auth.py`
- Create: `rdk_maze_tuner/dashboard/routes/control.py`
- Create: `rdk_maze_tuner/admin.py`
- Create: `rdk_maze_tuner/tests/test_auth.py`
- Create: `rdk_maze_tuner/tests/test_control_lease.py`
- Modify: `rdk_maze_tuner/dashboard/app.py`
- Modify: `requirements.txt`

- [ ] **2.1 写账户和双会话失败测试**

覆盖：

- Argon2 密码验证。
- opaque session token 只以摘要形式保存。
- Secure/HttpOnly/SameSite Cookie。
- CSRF token。
- 登录失败限速。
- 两个 TestClient 独立登录。
- 未登录 API 返回 401。

- [ ] **2.2 实现安全创建用户 CLI**

命令：

```bash
.venv/bin/python -m rdk_maze_tuner.admin create-user
```

密码通过 TTY 安全输入两次，不接受命令行明文参数，不写日志。

- [ ] **2.3 写控制权失败测试**

覆盖：

- 第一名用户取得控制权。
- 第二名用户只能观看。
- 5 秒心跳、15 秒过期。
- 持有人释放和过期后重新取得。
- 所有用户都能急停。
- 非持有人不能 start/pause/stop/reset/mode/apply-param。

- [ ] **2.4 实现 `ControlLeaseService`**

租约保存在 SQLite，所有状态改变 API 在服务端校验，不依赖前端按钮状态。

- [ ] **2.5 接入 FastAPI 并回归**

在 `requirements.txt` 明确加入 `argon2-cffi`；鉴权实现不得退回明文、可逆加密或通用快速哈希。

```bash
.venv/bin/python -m pytest rdk_maze_tuner/tests/test_auth.py rdk_maze_tuner/tests/test_control_lease.py -q
.venv/bin/python -m pytest rdk_maze_tuner/tests -q
```

- [ ] **2.6 提交检查点**

```text
feat: add authenticated control lease
```

### Task 3：单一设备会话和模式适配器

**文件：**

- Create: `rdk_maze_tuner/core/device_session.py`
- Create: `rdk_maze_tuner/platform/modes/__init__.py`
- Create: `rdk_maze_tuner/platform/modes/base.py`
- Create: `rdk_maze_tuner/platform/modes/simulation.py`
- Create: `rdk_maze_tuner/platform/modes/real.py`
- Create: `rdk_maze_tuner/tests/test_device_session.py`
- Create: `rdk_maze_tuner/tests/test_mode_adapters.py`
- Modify: `rdk_maze_tuner/core/serial_client.py`
- Modify: `rdk_maze_tuner/dashboard/runtime.py`
- Modify: `rdk_maze_tuner/dashboard/state.py`

- [ ] **3.1 写单 reader 失败测试**

验证：

- 一个后台 reader 独占 transport。
- ack 按 `seq` 唤醒对应 waiter。
- done/error 按 `action_id` 唤醒对应 waiter。
- telemetry 广播给状态订阅者。
- 并发 heartbeat 和 action 不互相抢读。
- 断开后所有 waiter 得到明确异常。

- [ ] **3.2 实现 `DeviceSession`**

保留 `SerialClient` 外部协议语义，但 Dashboard、MazeRunner 和 heartbeat 不再各自读取底层 stream。

- [ ] **3.3 定义 `ModeAdapter`**

统一方法：

```text
preflight()
reset(map_version, param_version)
start()
pause()
stop()
estop()
clear_estop()
snapshot()
close()
```

仿真适配器连接 `127.0.0.1:8765`；实车适配器第一步只实现离线占位和明确的 `DEVICE_OFFLINE`。

- [ ] **3.4 保留现有协议回归**

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_device_session.py \
  rdk_maze_tuner/tests/test_mode_adapters.py \
  rdk_maze_tuner/tests/test_serial_client.py \
  rdk_maze_tuner/tests/test_dashboard.py -q
```

- [ ] **3.5 提交检查点**

```text
refactor: centralize device session ownership
```

### Task 4：任务状态机与自动探索编排

**文件：**

- Create: `rdk_maze_tuner/platform/task_state.py`
- Create: `rdk_maze_tuner/platform/task_orchestrator.py`
- Create: `rdk_maze_tuner/dashboard/routes/tasks.py`
- Create: `rdk_maze_tuner/tests/test_task_state.py`
- Create: `rdk_maze_tuner/tests/test_task_orchestrator.py`
- Modify: `rdk_maze_tuner/core/maze_runner.py`
- Modify: `rdk_maze_tuner/dashboard/app.py`
- Modify: `rdk_maze_tuner/dashboard/state.py`

- [ ] **4.1 写状态转换失败测试**

覆盖：

- `IDLE → PREFLIGHT → READY → RUNNING → FINALIZING → COMPLETED`
- `RUNNING → PAUSING → PAUSED`
- `RUNNING → LOST/ERROR/ESTOP`
- 只有 IDLE/COMPLETED 可切换模式。
- LOST/ESTOP 不允许自动续跑。
- reset 创建新 run，不重用旧 run ID。

- [ ] **4.2 使 `MazeRunner` 支持可取消逐步运行**

新增：

- 每步前检查 pause/stop token。
- 每步产生结构化事件。
- planner 返回 stop 时区分“任务完成”“无路可走”。
- goal 条件由任务配置明确传入。

- [ ] **4.3 实现 Task API**

```text
POST /api/tasks
POST /api/tasks/{id}/preflight
POST /api/tasks/{id}/reset
POST /api/tasks/{id}/start
POST /api/tasks/{id}/pause
POST /api/tasks/{id}/stop
POST /api/tasks/{id}/estop
```

- [ ] **4.4 仿真闭环测试**

使用 fake `ModeAdapter` 和当前 SimEngine 验证点击 start 后自动执行直到完成，并生成事件。

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_task_state.py \
  rdk_maze_tuner/tests/test_task_orchestrator.py \
  rdk_maze_tuner/tests/test_maze_runner.py -q
```

- [ ] **4.5 提交检查点**

```text
feat: orchestrate safe maze tasks
```

### Task 5：主控制台界面 V2

**文件：**

- Modify: `rdk_maze_tuner/dashboard/templates/index.html`
- Modify: `rdk_maze_tuner/dashboard/static/app.css`
- Modify: `rdk_maze_tuner/dashboard/static/app.js`
- Create: `rdk_maze_tuner/dashboard/static/api.js`
- Create: `rdk_maze_tuner/dashboard/static/state.js`
- Create: `rdk_maze_tuner/dashboard/static/render.js`
- Create: `rdk_maze_tuner/dashboard/static/controls.js`
- Create: `rdk_maze_tuner/tests/test_dashboard_ui.py`

- [ ] **5.1 写 DOM 合同失败测试**

页面必须包含：

- 模式切换。
- 控制权状态。
- 急停。
- Webots/实车画面容器。
- 开始、暂停、停止、重置。
- 迷宫格、连续坐标、航向和置信度。
- 参数技术工作台。
- 实时地图和事件时间轴。

- [ ] **5.2 按已批准视觉稿重构静态页面**

保持原生 JavaScript；把 `app.js` 收敛为模块入口，具体逻辑拆入新文件，避免继续扩大单一文件。

- [ ] **5.3 接入鉴权、控制权和任务 WebSocket**

WebSocket 只推送增量或节流后的状态，页面目标更新 10–20 Hz。断线显示 LOST，不自动重发 start。

- [ ] **5.4 响应式验收**

至少检查：

- 1440×900 桌面。
- 1280×800 笔记本。
- 768px 宽只读观看。

- [ ] **5.5 运行测试**

```bash
.venv/bin/python -m pytest rdk_maze_tuner/tests/test_dashboard.py rdk_maze_tuner/tests/test_dashboard_ui.py -q
```

- [ ] **5.6 提交检查点**

```text
feat: build dual-mode control console
```

阶段 A 验收：

- 两名用户可以登录并同时观看。
- 只有租约持有人能重置和开始。
- Webots 自动探索从页面启动并完成。
- 急停对两名用户都有效。
- 原有 57 个测试无回归。

---

## 阶段 B：地图、定位、成绩和回放

### Task 6：规则迷宫描线编辑器与地图版本

**文件：**

- Create: `rdk_maze_tuner/core/maze_definition.py`
- Create: `rdk_maze_tuner/core/maze_validation.py`
- Create: `rdk_maze_tuner/platform/map_repository.py`
- Create: `rdk_maze_tuner/dashboard/routes/maps.py`
- Create: `rdk_maze_tuner/dashboard/static/maze_editor.js`
- Create: `rdk_maze_tuner/dashboard/static/maze_editor.css`
- Create: `simulation/webots/maze_car/map_loader.py`
- Modify: `simulation/webots/maze_car/controllers/maze_sim_controller/maze_sim_controller.py`
- Modify: `simulation/webots/maze_car/controllers/maze_sim_controller/sim_engine.py`
- Modify: `rdk_maze_tuner/core/maze_map.py`
- Create: `rdk_maze_tuner/tests/test_maze_definition.py`
- Create: `rdk_maze_tuner/tests/test_maze_editor_api.py`
- Create: `rdk_maze_tuner/tests/test_webots_map_loader.py`

- [ ] **6.1 写地图 schema 和校验失败测试**

覆盖：

- rows/cols、格尺寸、墙厚、墙高。
- 水平/垂直墙段。
- 起点、终点和初始方向。
- 外边界闭合。
- 零长度/重复墙拒绝。
- 起终点可达。
- 相同内容生成相同 digest。

- [ ] **6.2 实现不可变 `MapVersion`**

旧 run 继续引用旧地图；保存新版本不覆盖历史。

- [ ] **6.3 实现格线吸附编辑器**

支持：

- 拖动画墙。
- 擦除。
- 撤销/重做。
- 起点、终点、车头方向。
- 导入图片作为本地预览底图和比例标定。

原始图片存为 artifact；规划只读取结构化墙体。

- [ ] **6.4 动态装载 Webots 墙体**

由 Supervisor 根据 `MapVersion` 生成墙体和边界；不再依赖硬编码 `INTERNAL_WALLS`。仿真 reset 后位置、地图和视觉墙体一致。

仿真管理消息固定为：

```json
{"type":"load_map","seq":21,"map_version_id":"map-v3","digest":"...","definition":{}}
{"type":"reset","seq":22,"map_version_id":"map-v3"}
```

仿真桥必须回传包含相同 `seq`、`map_version_id` 和实际 digest 的 ack。只允许在非 RUNNING 状态加载地图；digest 不一致时拒绝 start。

- [ ] **6.5 运行测试**

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_maze_definition.py \
  rdk_maze_tuner/tests/test_maze_editor_api.py \
  rdk_maze_tuner/tests/test_webots_map_loader.py \
  rdk_maze_tuner/tests/test_webots_sim_bridge.py -q
```

- [ ] **6.6 提交检查点**

```text
feat: add versioned maze drawing editor
```

### Task 7：连续位姿、方向、置信度和滑移估算

**文件：**

- Create: `rdk_maze_tuner/core/pose_types.py`
- Create: `rdk_maze_tuner/core/pose_fusion.py`
- Create: `rdk_maze_tuner/core/slip_estimator.py`
- Create: `rdk_maze_tuner/tests/test_pose_fusion.py`
- Create: `rdk_maze_tuner/tests/test_slip_estimator.py`
- Modify: `rdk_maze_tuner/core/protocol.py`
- Modify: `simulation/webots/maze_car/controllers/maze_sim_controller/sim_engine.py`
- Modify: `rdk_maze_tuner/dashboard/state.py`
- Modify: `esp32_firmware/include/protocol.h`
- Modify: `esp32_firmware/src/protocol.cpp`
- Create: `esp32_firmware/include/imu.h`
- Create: `esp32_firmware/src/imu.cpp`
- Modify: `esp32_firmware/src/main.cpp`
- Modify: `esp32_firmware/platformio.ini`

- [ ] **7.1 写仿真姿态失败测试**

模拟编码器预测、IMU yaw rate、ToF 墙面约束和 Webots 真值；断言真值只进入评估器，不进入融合输入。

- [ ] **7.2 实现双层位置**

输出：

- grid cell 和主方向。
- `x_mm`、`y_mm`、`yaw_deg`。
- covariance、confidence、correction source。

- [ ] **7.3 实现滑移指标**

比较编码器预测与 IMU/ToF 外部运动证据，输出左右滑移率、等效摩擦档案和质量标志。实车界面不把估算值标为物理真值。

- [ ] **7.4 增加 IMU 可用性合同**

先实现可编译的可选 `ImuSource`：

- 未配置硬件时 `imu_available=false`。
- 仿真提供确定性 IMU 数据。
- 真实模块和引脚必须在实物核对后配置。
- 未接 IMU 时允许格子定位降级运行，但连续航向验收失败。

- [ ] **7.5 运行 Python 与 PlatformIO 验证**

```bash
.venv/bin/python -m pytest rdk_maze_tuner/tests/test_pose_fusion.py rdk_maze_tuner/tests/test_slip_estimator.py -q
.venv/bin/python -m pytest rdk_maze_tuner/tests -q
(cd esp32_firmware && ../.venv/bin/pio run)
```

- [ ] **7.6 提交检查点**

```text
feat: add fused pose and slip evidence
```

### Task 8：成绩、视频、时间轴和同步回放

**文件：**

- Create: `rdk_maze_tuner/platform/scoring.py`
- Create: `rdk_maze_tuner/platform/replay.py`
- Create: `rdk_maze_tuner/platform/video_recorder.py`
- Create: `rdk_maze_tuner/platform/retention.py`
- Create: `rdk_maze_tuner/dashboard/routes/runs.py`
- Create: `rdk_maze_tuner/dashboard/static/replay.js`
- Create: `rdk_maze_tuner/config/score_profile_v1.yaml`
- Create: `rdk_maze_tuner/tests/test_scoring.py`
- Create: `rdk_maze_tuner/tests/test_replay.py`
- Create: `rdk_maze_tuner/tests/test_retention.py`
- Modify: `rdk_maze_tuner/core/logger.py`

- [ ] **8.1 写原始指标和计分失败测试**

原始指标独立保存；更换 score profile 只重算综合分，不改原始数据。

- [ ] **8.2 实现默认 `score-profile-v1`**

权重：

- 完成与终点 35。
- 地图准确率 20。
- 定位与方向 15。
- 路径与时间 12。
- 动作精度 8。
- 安全与稳定 10。

- [ ] **8.3 实现视频记录**

仿真：

- 从 Xvfb/Webots 画面记录。

实车：

- 接收 RDK 上传的 640×360、5–10 FPS JPEG。
- 服务端限制 3 Mbps。
- ffmpeg 封装为 MP4。

视频失败时任务继续，但 run 标记媒体不完整。

- [ ] **8.4 实现统一回放清单**

按单调时间同步视频、轨迹、遥测、动作、参数、审批和安全事件；关键事件可以直接跳转。

- [ ] **8.5 实现保留策略**

- 普通视频 30 天。
- JSONL/指标 180 天。
- 最佳、基准、事故和比赛证据长期保留。

- [ ] **8.6 运行测试**

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_scoring.py \
  rdk_maze_tuner/tests/test_replay.py \
  rdk_maze_tuner/tests/test_retention.py -q
```

- [ ] **8.7 提交检查点**

```text
feat: add scored synchronized run replay
```

阶段 B 验收：

- 手绘地图能生成 Webots 墙体并完成一次任务。
- 页面显示格子、连续位置、方向和置信度。
- 结束后生成原始指标、综合分、JSONL 和同步回放。
- 任一 run 可恢复对应地图和配置。

---

## 阶段 C：参数资产与安全调参

### Task 9：不可变参数版本和安全策略

**文件：**

- Create: `rdk_maze_tuner/core/param_policy.py`
- Create: `rdk_maze_tuner/core/local_rule_tuner.py`
- Create: `rdk_maze_tuner/platform/param_repository.py`
- Create: `rdk_maze_tuner/dashboard/routes/params.py`
- Create: `rdk_maze_tuner/tests/test_param_policy.py`
- Create: `rdk_maze_tuner/tests/test_param_versions.py`
- Modify: `rdk_maze_tuner/core/param_manager.py`
- Modify: `rdk_maze_tuner/core/auto_tuner.py`
- Modify: `rdk_maze_tuner/tests/test_auto_tuner.py`
- Modify: `rdk_maze_tuner/config/limits.yaml`
- Modify: `rdk_maze_tuner/config/params.yaml`

- [ ] **9.1 先写安全域失败测试**

明确拒绝自动修改：

- `tof.front_stop_mm`
- `tof.danger_stop_mm`
- `motor.max_pwm`
- `safety.*`
- 参数绝对范围

当前 `test_auto_tuner_clamps_to_limits` 必须改为验证安全建议被拒绝，而不是修改 `front_stop_mm`。

- [ ] **9.2 实现参数分类**

分类：

- `safety`
- `motion`
- `estimation`
- `simulation_physics`
- `ui_only`

每个参数记录单位、范围、自动权限和适用模式。

- [ ] **9.3 实现不可变 `ParamVersion`**

保存父版本、完整快照、diff、来源、证据、审批、车体、地图、地面、固件和代码版本。

- [ ] **9.4 实现实车 5% 规则补偿**

RDK `LocalRuleTuner`：

- 单次相对变化不超过 5%。
- 同时受绝对范围限制。
- 观察窗口恶化自动回滚。
- 通过 `ParamApplier` 下发，ESP32 再次限幅。

- [ ] **9.5 实现手动审批和恢复 API**

```text
POST /api/params/candidates
POST /api/params/candidates/{id}/simulate
POST /api/params/candidates/{id}/approve
POST /api/params/versions/{id}/restore
```

- [ ] **9.6 运行测试**

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_param_policy.py \
  rdk_maze_tuner/tests/test_param_versions.py \
  rdk_maze_tuner/tests/test_auto_tuner.py \
  rdk_maze_tuner/tests/test_param_manager.py -q
```

- [ ] **9.7 提交检查点**

```text
feat: version safe tuning parameters
```

阶段 C 验收：

- 任一参数变化都有版本、证据和审批。
- 自动调参不能触碰安全域。
- 实车规则补偿受 5% 限制并可回滚。
- 高分参数按车体/地面/地图/固件隔离。

---

## 阶段 D：RDK 实车 Agent

### Task 10：RDK Agent、设备认证和真实模式

**文件：**

- Create: `rdk_maze_tuner/agent/__init__.py`
- Create: `rdk_maze_tuner/agent/config.py`
- Create: `rdk_maze_tuner/agent/client.py`
- Create: `rdk_maze_tuner/agent/runtime.py`
- Create: `rdk_maze_tuner/agent/video.py`
- Create: `rdk_maze_tuner/agent/main.py`
- Create: `rdk_maze_tuner/dashboard/routes/agents.py`
- Create: `rdk_maze_tuner/platform/device_tokens.py`
- Create: `rdk_maze_tuner/tests/test_agent_protocol.py`
- Create: `rdk_maze_tuner/tests/test_agent_runtime.py`
- Create: `rdk_maze_tuner/tests/test_device_tokens.py`
- Create: `deploy/rdk/maze-agent.service`
- Create: `deploy/rdk/install_agent.sh`
- Create: `deploy/rdk/maze-agent.env.example`
- Modify: `requirements.txt`
- Modify: `rdk_maze_tuner/platform/modes/real.py`

- [ ] **10.1 写设备注册和 WSS 握手失败测试**

令牌要求：

- 只保存摘要。
- 绑定 device ID。
- 可以吊销和轮换。
- 与网站 session 分离。

- [ ] **10.2 实现 Agent 出站连接**

RDK 主动连接：

```text
wss://8.ilelezhan.cn/ws/agents/{device_id}
```

支持指数退避、心跳、断线状态和消息幂等。

在 `requirements.txt` 明确加入 `websockets`，生产连接必须验证系统 CA、域名和证书有效期，不提供跳过 TLS 校验的配置。

- [ ] **10.3 将共享核心放在 RDK 本地运行**

实车模式下 RDK 本地运行：

- `DeviceSession`
- `PoseFusion`
- `MazeMap`
- `MazePlanner`
- `MazeRunner`
- `LocalRuleTuner`
- `ParamApplier`

服务器只发送任务级命令和已审批版本。

- [ ] **10.4 实现断云停车**

Agent 失去服务器连接后：

1. 立即请求 ESP32 stop。
2. 继续本地串口心跳只到安全停车确认。
3. 停止新动作。
4. 服务器重连后进入 LOST，禁止自动续跑。

- [ ] **10.5 实现视频上传抽象**

提供：

- `FakeFrameSource` 用于测试。
- `FileFrameSource` 用于录制样本回归。
- `V4L2/GStreamerFrameSource` 通过配置接入真实 RDK 相机。

具体相机 pipeline 在读取真实 RDK 设备节点后写入本机环境配置，不硬编码进仓库。

- [ ] **10.6 安装脚本和 systemd**

RDK service：

- 非 root 运行。
- 设备令牌只在权限 0600 的环境文件。
- `Restart=on-failure`。
- 串口设备通过显式配置。

- [ ] **10.7 无硬件集成测试**

使用 fake serial + 本地 WSS 测试完整真实模式，不要求连接物理设备。

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_agent_protocol.py \
  rdk_maze_tuner/tests/test_agent_runtime.py \
  rdk_maze_tuner/tests/test_device_tokens.py -q
```

- [ ] **10.8 物理硬件门**

需要用户提供并确认：

- RDK X3 当前系统和网络。
- ESP32 串口设备路径。
- 摄像头设备/pipeline。
- IMU 模块和接线。

按 AGENTS.md 的硬件验证顺序执行。未经确认不烧录。

- [ ] **10.9 提交检查点**

```text
feat: connect authenticated rdk agent
```

阶段 D 验收：

- RDK 可以从局域网主动连到域名。
- 网站切换真实模式后能看到设备状态和视频。
- 自动探索在 RDK 本地决策。
- 断云和断串口均能停车。
- 实车验收结果与仿真证据分开记录。

---

## 阶段 E：模型建议和仿真优化

### Task 11：可插拔模型 Advisor

**文件：**

- Create: `rdk_maze_tuner/advisor/__init__.py`
- Create: `rdk_maze_tuner/advisor/base.py`
- Create: `rdk_maze_tuner/advisor/schemas.py`
- Create: `rdk_maze_tuner/advisor/openai_compatible.py`
- Create: `rdk_maze_tuner/platform/advisor_service.py`
- Create: `rdk_maze_tuner/dashboard/routes/advisor.py`
- Create: `rdk_maze_tuner/tests/test_advisor_schema.py`
- Create: `rdk_maze_tuner/tests/test_advisor_service.py`
- Create: `deploy/server/maze-platform.env.example`
- Modify: `requirements.txt`

- [ ] **11.1 写模型输出拒绝测试**

拒绝：

- 无效 JSON。
- 未知参数。
- 越界值。
- 安全参数。
- 无证据建议。
- 提示注入式日志内容。
- 提供商超时。

- [ ] **11.2 实现 `AdvisorProvider` 接口**

第一版使用 `httpx` 实现 OpenAI-compatible HTTP adapter，通过配置支持不同服务端点；在 `requirements.txt` 明确加入 `httpx`，不在代码中写入任何提供商密钥。

- [ ] **11.3 实现结构化候选**

模型输出只允许：

- changes。
- evidence。
- expected effect。
- risk。
- recommended trials。

候选默认状态为 `proposed`，实车永远需要人工批准。

- [ ] **11.4 接入仿真试跑**

页面“仿真试跑 N 次”创建 optimizer batch，而不是直接下发实车。

- [ ] **11.5 运行测试**

```bash
.venv/bin/python -m pytest rdk_maze_tuner/tests/test_advisor_schema.py rdk_maze_tuner/tests/test_advisor_service.py -q
```

- [ ] **11.6 提交检查点**

```text
feat: add guarded model tuning advisor
```

### Task 12：CPU 仿真批量优化和强化学习环境

**文件：**

- Create: `requirements-optimizer.txt`
- Create: `rdk_maze_tuner/optimizer/__init__.py`
- Create: `rdk_maze_tuner/optimizer/scenarios.py`
- Create: `rdk_maze_tuner/optimizer/objective.py`
- Create: `rdk_maze_tuner/optimizer/optuna_runner.py`
- Create: `rdk_maze_tuner/optimizer/gym_env.py`
- Create: `rdk_maze_tuner/tests/test_optimizer_objective.py`
- Create: `rdk_maze_tuner/tests/test_optimizer_promotion.py`
- Create: `rdk_maze_tuner/tests/test_gym_env.py`
- Create: `deploy/server/systemd/maze-optimizer.service`

- [ ] **12.1 定义场景矩阵**

`requirements-optimizer.txt` 固定包含 `optuna` 和 `gymnasium`，与基础网站依赖分离；服务器未安装优化依赖时不影响主平台启动。

至少覆盖：

- 多地图。
- `μ ∈ [0.35, 0.80]`。
- 三档传感器噪声。
- 左右轮增益偏差。
- 轮滑、障碍、定位丢失和超时。

- [ ] **12.2 实现 Optuna 目标函数**

先优化低维运动和估计参数。每个 trial：

- 使用独立 run 和参数候选。
- 有硬超时。
- 记录所有原始指标。
- 任何安全违规直接判失败。

- [ ] **12.3 实现晋级规则**

候选必须：

- 场景矩阵零安全违规。
- 平均分改善。
- 最差分不低于稳定版容差。
- 有足够样本。

通过后只进入 `simulation_passed`，不自动成为实车稳定版。

- [ ] **12.4 提供 Gymnasium 风格环境**

第一版只输出高层动作空间：

- move cell。
- turn left/right/back。
- stop。

不输出 PWM。环境测试验证 reset、step、reward 和终止条件；不在第一版承诺训练出可上实车的强化学习策略。

- [ ] **12.5 CPU smoke test**

在服务器 headless 模式运行 20-trial 小批次，记录总耗时、CPU、内存和最佳候选。

- [ ] **12.6 提交检查点**

```text
feat: add headless parameter optimization
```

阶段 E 验收：

- 模型只能生成受 schema 和安全策略约束的候选。
- 未配置模型密钥时基础平台完全可用。
- CPU 服务器能运行小批量 headless 优化。
- 强化学习环境只使用高层动作，不进入 PWM 闭环。

---

## 阶段 F：Caddy、公网部署和最终验收

### Task 13：HTTPS、Caddy 和生产服务

**文件：**

- Create: `deploy/server/Caddyfile`
- Create: `deploy/server/install_platform.sh`
- Create: `deploy/server/systemd/maze-platform.service`
- Create: `deploy/server/systemd/maze-retention.service`
- Create: `deploy/server/systemd/maze-retention.timer`
- Modify: `deploy/server/systemd/maze-optimizer.service`
- Modify: `deploy/server/deploy_release.sh`
- Modify: `deploy/server/install_host.sh`
- Modify: `deploy/server/rollback_release.sh`
- Modify: `README.md`

- [ ] **13.1 先做本机反向代理测试**

验证：

- `/`、`/api`、`/ws`。
- `/simulation` HTTP 和 WebSocket。
- 未登录不能读取 Webots 流。
- noVNC 不在公网路由。

- [ ] **13.2 安装 Caddy**

实施时从官方源核对当前安装方法。Caddy：

- 自动签发和续期 `8.ilelezhan.cn` 证书。
- 80 跳转 443。
- 443 代理 FastAPI。
- `/simulation/*` 先通过 Caddy `forward_auth` 调用 FastAPI `GET /api/auth/authorize`，成功后再反向代理到 `127.0.0.1:1234`；不采用第二套 Webots 登录。
- FastAPI 鉴权端点只返回鉴权结果和最小用户标识，不返回 session token；Caddy 不记录 Cookie。
- 设置安全响应头和上传大小限制。

- [ ] **13.3 更新 systemd**

`maze-platform.service`：

- User/Group 为 `maze`。
- 环境文件权限 0600。
- 数据目录 `/srv/maze/shared`。
- Dashboard 继续监听 `127.0.0.1:8000`。
- 模型失败不触发整个平台重启循环。

- [ ] **13.4 更新 UFW**

只增加：

```text
22/tcp
80/tcp
443/tcp
```

保持 SSH 密码登录现状，不部署会关闭密码登录的 hardening 变更。

发布前后都运行 `sshd -T | grep -i '^passwordauthentication'`，验收结果必须保持 `passwordauthentication yes`。

- [ ] **13.5 发布与健康检查**

`deploy_release.sh` 依次检查：

- 数据库迁移。
- Python compile。
- Python tests。
- Dashboard loopback。
- Webots stream loopback。
- Caddy config validate。
- HTTPS `/api/health`。

失败自动恢复上一 release 和服务配置。

- [ ] **13.6 生产只读验证**

```bash
curl -fsSI https://8.ilelezhan.cn/
curl -fsS https://8.ilelezhan.cn/api/health
ssh maze-cvm 'sudo ufw status verbose'
ssh maze-cvm 'ss -ltn'
```

确认内部端口没有公网暴露。

- [ ] **13.7 提交检查点**

```text
deploy: publish authenticated maze platform
```

### Task 14：双用户 E2E、故障注入和交付证据

**文件：**

- Modify: `requirements-dev.txt`
- Create: `rdk_maze_tuner/tests/e2e/test_two_user_flow.py`
- Create: `rdk_maze_tuner/tests/e2e/test_simulation_run.py`
- Create: `rdk_maze_tuner/tests/e2e/test_replay_flow.py`
- Create: `rdk_maze_tuner/tests/test_fault_injection.py`
- Create: `docs/acceptance/dual-mode-platform-checklist.md`
- Create: `docs/operations/platform-runbook.md`

- [ ] **14.1 浏览器 E2E**

使用两个独立浏览器上下文验证：

- 两人登录。
- 一人控制、一人观看。
- 观看者控制 API 被拒绝。
- 两人都能急停。
- 模式切换保护。
- start 到 completed。
- 成绩和回放。

- [ ] **14.2 故障注入**

覆盖：

- Webots 重启。
- Dashboard 重启。
- RDK Agent 掉线。
- 串口断开。
- malformed telemetry。
- 定位置信度下降。
- 磁盘接近阈值。
- 模型超时和恶意建议。

- [ ] **14.3 性能记录**

记录而不是猜测：

- 页面遥测 Hz。
- Webots FPS。
- 浏览器/服务器延迟。
- CPU、RAM 和磁盘。
- 视频带宽。
- stop/estop/断联停车时间。

- [ ] **14.4 完整回归**

```bash
.venv/bin/python -m compileall -q rdk_maze_tuner simulation
.venv/bin/python -m pytest rdk_maze_tuner/tests -q
(cd esp32_firmware && ../.venv/bin/pio run)
```

- [ ] **14.5 服务器回归**

在新 release 目录运行同样测试，检查 systemd、Caddy、证书和端口。

- [ ] **14.6 物理验收**

只有 RDK X3、ESP32、三路 ToF、编码器、摄像头和 IMU 接入后执行。按 AGENTS.md 顺序，不跳过急停。

- [ ] **14.7 最终交付**

交付：

- 网站地址。
- 两名账户初始化方式。
- 控制权说明。
- RDK Agent 安装说明。
- 参数版本和回滚说明。
- 运维、备份、恢复和证据清单。

- [ ] **14.8 最终提交**

```text
test: complete dual-mode platform acceptance
```

---

## 执行节奏

每次只执行一个 Task；完成后必须：

1. 展示失败测试证据。
2. 实现最小代码。
3. 展示目标测试和完整回归。
4. 检查 `git diff --check`。
5. 检查是否出现秘密、真实设备路径或公网凭据。
6. 提交独立 commit。
7. 更新本计划复选框和验收证据。

推荐交付顺序：

```text
M1：Task 1–5  网站 + Webots 一键仿真
M2：Task 6–8  描线地图 + 定位 + 成绩回放
M3：Task 9    参数资产和安全调参
M4：Task 10   RDK 实车模式
M5：Task 11–12 模型建议和 CPU 优化
M6：Task 13–14 公网发布与最终验收
```

## 完成定义

只有同时满足以下条件，整个计划才算完成：

- `8.ilelezhan.cn` 使用有效 HTTPS。
- 两名用户能同时观看且只有一人控制。
- 仿真模式一键运行到完成并回放。
- 真实模式由 RDK 本地决策并通过物理安全验收。
- 迷宫描线生成版本化结构地图和 Webots 墙体。
- 页面显示格子、连续位姿、方向、置信度和滑移证据。
- 参数候选可比较、审批、晋级和回滚。
- 模型和优化器不能越过安全域。
- 80/443/22 之外的内部端口不暴露。
- Python、浏览器、部署、故障注入和 PlatformIO 验证全部通过。
