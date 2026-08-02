# 地图终点权威、可靠到达与防空转导航实施计划

> 对应已确认设计：
> `docs/superpowers/specs/2026-08-02-map-goal-navigation-design.md`

**目标：** 自动探索始终使用不可变地图版本保存的终点；当前
`Task12 公网验收迷宫 · v2` 自动显示并锁定 `(4,0)`。小车沿合法路线运行，
只有在编码器、ToF、可用 IMU、墙面约束和融合定位共同确认实际到达后，才完成任务。
欠行程和可恢复偏航使用带 `action_id` 的有限动作级修正，空转、冲突或低置信度时
安全停车。

**插入位置：** 本计划是当前 Webots 物理迷宫的 P5 增量，优先于继续扩展模型调参、
强化学习或大模型建议。完成 Task 1–13 后，才能发布“仿真已实现走到终点”。

**技术路线：**

- `MapGoalResolver` 从 `MapRepository` 的不可变版本解析主终点。
- `TaskOrchestrator` 保存 `run_kind`、终点来源、地图摘要和完成阈值快照。
- `GoalDirectedPlanner` 使用确定性 BFS 生成到地图终点的动作路线。
- `MazeMap` 分离计划墙体和实时观测墙体，传感器不能改写不可变地图。
- `TaskPoseTracker` 复用现有 `PoseFusion`、`SlipEstimator` 和墙面约束。
- `MotionEvidenceGate` 决定动作是接受、可恢复还是不安全。
- `nudge_forward` 和 `align_heading` 仍是动作级协议，不引入云端高频 PWM。
- 新增 P5 候选验收，再执行正式站点浏览器验收和原子发布。

## 当前基线与证据边界

计划编写时：

- 当前分支为 `main`，设计提交为 `e5f33e1`。
- 现有自动页面仍从 `goalX/goalY` 构造 `goal.type=cell`。
- `TaskOrchestrator._goal_for` 仍只比较 `maze.position == cell`。
- `MazeRunner` 仍在动作 `done` 后直接推进逻辑地图。
- `MazeMap.observe` 仍可能覆盖从地图定义加载的墙体。
- 现有 `PoseFusion`、`SlipEstimator`、单一 `DeviceSession` 和 Webots
  P1–P4 验收可以复用。
- 当前目录中未跟踪的 `.vscode/` 和 `去年程序备份/` 属于用户内容，不纳入任何提交。

本计划不把历史测试数量、旧 release 或旧生产截图当作当前通过证据。每个 Task 都要
重新记录实际命令和结果。

## 不可破坏的约束

1. 自动任务的终点只能来自任务保存的不可变地图版本。
2. 页面和直接 API 都不能用手工坐标覆盖自动终点。
3. RDK/服务器不发送 5–10 ms 级左右轮 PWM。
4. 每个正常动作和修正动作都有唯一 `action_id`，并等待对应 `done/error`。
5. `sim_truth` 只用于评估、评分和 P5 证据，不进入规划、融合或动作证据门。
6. 逻辑格子只在动作物理证据通过后推进；`error`、超时和低置信度不推进。
7. 自动调参不能修改安全参数或 `arrival_verification` 完成阈值。
8. 地图与传感器冲突时停车，不自动修改地图或降低阈值。
9. 急停、心跳超时、前方过近、动作超时和 ESP32 本地限幅保持最高优先级。
10. 仿真 P5 通过不等于真实小车通过。
11. 不在没有新鲜设备确认时烧录 ESP32。
12. 不把密码、密钥、服务器地址、真实串口路径或其他敏感信息写入源码、测试和计划。

## 每个 Task 的固定执行协议

每次只执行一个 Task：

1. 确认 `git status --short` 只包含预期改动和已知用户未跟踪目录。
2. 先写测试并运行，保存明确 RED 证据。
3. 只实现使当前 Task 测试通过的最小代码。
4. 运行该 Task 的目标测试。
5. 运行完整 Python、Dashboard 和 PlatformIO 回归。
6. 涉及 Webots 控制或物理模型时，在隔离服务器执行对应 P1–P4。
7. 运行 `git diff --check`。
8. 检查暂存差异中没有凭据、真实设备路径或无关文件。
9. 把 RED、目标测试、完整回归和仿真/硬件边界写入本计划的实施记录。
10. 独立提交；前一个 Task 未通过不得开始下一个 Task。

通用本地回归：

```bash
.venv/bin/python -m compileall -q rdk_maze_tuner simulation
.venv/bin/python -m pytest rdk_maze_tuner/tests -q
/Users/yukii/.platformio/penv/bin/pio run -d esp32_firmware
node --check rdk_maze_tuner/dashboard/static/api.js
node --check rdk_maze_tuner/dashboard/static/state.js
node --check rdk_maze_tuner/dashboard/static/render.js
node --check rdk_maze_tuner/dashboard/static/controls.js
node --check rdk_maze_tuner/dashboard/static/replay.js
git diff --check
```

如果本机可执行路径变化，记录实际路径，不通过伪造软链接绕过检查。

---

## 阶段 A：终点权威

### Task 1：建立不可变地图终点解析器

**文件：**

- Create: `rdk_maze_tuner/platform/map_goal_resolver.py`
- Create: `rdk_maze_tuner/tests/test_map_goal_resolver.py`
- Modify: `rdk_maze_tuner/platform/__init__.py`

#### 1.1 写失败测试

覆盖：

- 单终点地图解析出终点、候选列表、地图版本和地图摘要。
- 多终点选择从起点最短可达的终点。
- 路径长度相同时按 `(y, x)` 升序决胜。
- 地图没有终点时返回 `MAP_GOAL_MISSING`。
- 所有终点不可达时返回 `MAP_GOAL_UNREACHABLE`。
- `MapVersion.digest` 与 `definition.content_digest` 不一致时返回
  `MAP_DIGEST_MISMATCH`。
- 解析器不写数据库、不修改地图定义。

Run：

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_map_goal_resolver.py -q
```

Expected RED：`MapGoalResolver` 尚不存在。

#### 1.2 最小实现

定义冻结结果：

```text
ResolvedMapGoal
├── cell
├── candidate_cells
├── source_map_version
├── source_map_digest
├── resolution
└── path_length_cells
```

解析器只通过注入的 `MapRepository.get_version` 读取版本。最短路径使用地图定义中的
规则格墙体，不使用传感器和 Webots 真值。

#### 1.3 验证与提交

运行目标测试和固定完整回归。

Commit：

```text
feat: resolve automatic goals from immutable maps
```

**Task 1 实施记录（2026-08-02）：**

- RED：`.venv/bin/python -m pytest rdk_maze_tuner/tests/test_map_goal_resolver.py -q`
  在收集阶段失败，报错
  `ModuleNotFoundError: No module named 'rdk_maze_tuner.platform.map_goal_resolver'`。
- 目标测试：同一命令实现后 `8 passed in 0.03s`。
- Python 语法：`.venv/bin/python -m compileall -q rdk_maze_tuner simulation`
  通过。
- Python 完整回归：`.venv/bin/python -m pytest rdk_maze_tuner/tests -q`
  为 `339 passed in 5.45s`。
- Dashboard 语法：`api.js`、`state.js`、`render.js`、`controls.js`、
  `replay.js` 全部通过 `node --check`。
- PlatformIO：`/Users/yukii/.platformio/penv/bin/pio run -d esp32_firmware`
  成功；RAM 6.9%（22744/327680 bytes），Flash 24.2%
  （317041/1310720 bytes）。
- 边界：本 Task 只新增不可变地图终点解析和纯逻辑最短路，未修改
  Webots 控制、固件行为或生产部署；未执行服务器仿真和真车验收。

### Task 2：把终点权威接入任务、API 和运行快照

**文件：**

- Modify: `rdk_maze_tuner/platform/task_orchestrator.py`
- Modify: `rdk_maze_tuner/dashboard/routes/tasks.py`
- Modify: `rdk_maze_tuner/dashboard/app.py`
- Modify: `rdk_maze_tuner/tests/test_task_orchestrator.py`
- Modify: `rdk_maze_tuner/tests/test_dashboard.py`
- Create: `rdk_maze_tuner/tests/test_task_goal_authority.py`

#### 2.1 写失败测试

覆盖：

- 网站创建任务默认 `run_kind=auto_to_map_goal`。
- 自动任务未提交 `goal` 时，后端解析并保存地图终点。
- 自动任务提交 `goal.type=cell` 时返回
  `400 AUTO_GOAL_OVERRIDE_FORBIDDEN`，且不写任务、run 或事件。
- 当前 v2 fixture 的任务终点为 `(4,0)`，不是 `(0,3)`。
- `task.created` 和任务 snapshot 包含 `run_kind`、终点来源、地图摘要和解析规则。
- 运行 metadata 保存完全相同的终点快照。
- 地图切换创建新任务；旧任务终点不变。
- 内部完整建图测试只有显式 `run_kind=exploration_complete` 才能使用旧语义。

Run：

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_task_goal_authority.py \
  rdk_maze_tuner/tests/test_task_orchestrator.py \
  rdk_maze_tuner/tests/test_dashboard.py -q
```

Expected RED：路由仍转发客户端 `goal`，任务记录没有 `run_kind` 和地图终点快照。

#### 2.2 最小实现

- `TaskRecord` 增加 `run_kind`。
- `TaskOrchestrator` 注入 `MapGoalResolver`。
- 自动任务先解析地图终点，再创建 `TaskRecord`。
- 为自动覆盖错误提供带稳定 code 的 `TaskValidationError`。
- `_insert_run_locked`、`_snapshot_locked` 和 `task.created` 使用同一终点快照。
- `create_app` 只创建一份 resolver 并注入 orchestrator。
- 旧测试必须显式声明旧运行类型，不允许测试绕过公开 API 规则。

#### 2.3 验证与提交

运行目标测试和固定完整回归。

Commit：

```text
fix: enforce map-owned automatic task goals
```

**Task 2 实施记录（2026-08-02）：**

- RED：目标命令得到 `19 failed, 18 passed in 1.42s`；关键失败为
  `TaskOrchestrator.__init__()` 不接受 `map_goal_resolver`、
  `create_task()` 不接受 `run_kind`，且 Dashboard state 没有共享解析器。
- 目标测试：实现后同一命令为 `37 passed in 1.79s`。
- 自动任务默认 `run_kind=auto_to_map_goal`，不接收客户端目标；当前
  Task12 v2 fixture 解析并冻结 `(4,0)`，路径长度为 8 格。
- `task.created`、任务 snapshot、run metadata 和运行 JSONL 保存同一
  `map_goal` 快照；地图新版本创建新任务，不修改旧任务快照。
- 客户端提交自动任务 `goal` 返回
  `400 AUTO_GOAL_OVERRIDE_FORBIDDEN`，且失败前后不新增 task、run 或 event。
- 旧生命周期测试已显式声明 `run_kind=exploration_complete`，不再依赖网站
  默认语义。
- Python 语法：`.venv/bin/python -m compileall -q rdk_maze_tuner simulation`
  通过。
- Python 完整回归：`.venv/bin/python -m pytest rdk_maze_tuner/tests -q`
  为 `345 passed in 5.45s`。
- Dashboard 语法：`api.js`、`state.js`、`render.js`、`controls.js`、
  `replay.js` 全部通过 `node --check`。
- PlatformIO：`/Users/yukii/.platformio/penv/bin/pio run -d esp32_firmware`
  成功；RAM 6.9%（22744/327680 bytes），Flash 24.2%
  （317041/1310720 bytes）。
- 边界：本 Task 未修改页面目标显示、规划器、Webots 控制或固件行为；
  未部署服务器，也未执行仿真运行和真车验收。

### Task 3：页面自动显示并锁定地图终点

**文件：**

- Modify: `rdk_maze_tuner/dashboard/templates/index.html`
- Modify: `rdk_maze_tuner/dashboard/static/api.js`
- Modify: `rdk_maze_tuner/dashboard/static/state.js`
- Modify: `rdk_maze_tuner/dashboard/static/controls.js`
- Modify: `rdk_maze_tuner/dashboard/static/render.js`
- Modify: `rdk_maze_tuner/dashboard/static/app.css`
- Modify: `rdk_maze_tuner/tests/test_dashboard_ui.py`
- Modify: `rdk_maze_tuner/tests/test_maze_editor_api.py`

#### 3.1 写失败测试

覆盖：

- 主面板标题为“地图终点（自动）”。
- `goalX/goalY` 不再是可编辑任务输入。
- 选择地图版本后调用 `getMapVersion` 并显示主终点、候选终点、摘要短码和路径长度。
- 当前 v2 fixture 显示 `X 4 / Y 0`。
- `taskDefinition()` 发送 `run_kind=auto_to_map_goal`，不构造 `goal.cell`。
- `taskDefinitionChanged` 比较模式、地图、参数和物理配置，不比较手工目标。
- 地图定义或终点加载失败时，预检和开始按钮禁用。
- 切换地图后不会用旧任务目标覆盖新地图终点。
- 单步调试区域显示“不会改变自动终点，也不会触发自动完成”。
- 急停入口始终可见。

Run：

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_dashboard_ui.py \
  rdk_maze_tuner/tests/test_maze_editor_api.py -q
```

Expected RED：页面仍显示可编辑的 `(0,3)` 并把它提交给自动任务。

#### 3.2 最小实现

- 地图版本详情进入前端状态，渲染只读终点。
- 主任务定义删除客户端目标。
- 地图加载状态纳入按钮禁用逻辑。
- 把手工坐标移动到单步调试区并更名为 `debugGoalX/debugGoalY`；坐标执行按钮在
  Task 10 前保持禁用并显示“坐标单步调试尚未接入”，现有方向动作按钮不受影响。

#### 3.3 浏览器验收

本地真实浏览器验证：

1. 登录并取得控制权。
2. 选择 v2。
3. 页面显示并锁定 `(4,0)`。
4. 浏览器请求体不包含 `(0,3)` 或 `goal.cell`。
5. 切换地图时终点和摘要同步变化。

#### 3.4 验证与提交

运行目标测试、JavaScript 语法检查和固定完整回归。

Commit：

```text
fix: lock automatic goals to the selected map
```

**Task 3 实施记录（2026-08-02）：**

- RED：运行目标命令得到 `4 failed, 11 passed in 0.90s`；失败分别证明主面板
  仍缺少只读地图终点证据，任务定义尚未发送 `run_kind`，选择器尚未加载地图详情，
  且旧任务目标仍可能参与页面状态。
- 最小实现：
  - `state.js` 保存所选不可变地图详情、加载状态、错误和解析后的 `mapGoal`，并按
    与后端一致的候选排序、BFS 最短距离和 `(y,x)` 决胜规则生成显示快照。
  - `controls.js` 仅提交 `run_kind=auto_to_map_goal`、地图版本、参数版本、模式和物理
    Profile；删除客户端 `goal`，地图终点未加载成功时拒绝创建任务。
  - `maze_editor.js` 的任务地图选择器优先恢复已保存选择，并在选择或加载版本后触发
    详情刷新，不再读取 `activeTask.goal`。
  - 主面板改为只读“地图终点（自动）”，显示主终点、候选终点、地图摘要和最短路径；
    手工坐标移入单步调试区，坐标执行按钮在 Task 10 前保持禁用。
  - `render.js` 把地图加载状态纳入“预检并重置”和“开始自动探索”的禁用条件；
    地图加载失败时清空证据并显示稳定错误。
- 目标测试：同一命令得到 `15 passed in 0.86s`。
- 本地真实浏览器验收（临时隔离数据库、`127.0.0.1:8765`）：
  - v2 显示并锁定 `X 4 / Y 0`、候选 `(4,0)`、摘要 `77259af666c2`、路径 `8 格`。
  - 实际 `POST /api/tasks` 请求体为
    `{"run_kind":"auto_to_map_goal","mode":"simulation","map_version":"mapv-v2",`
    `"param_version":"1","max_steps":500,"physical_profile_id":"normal-v1"}`，不含
    `goal` 或 `(0,3)`。
  - 切换 v1 后显示 `X 0 / Y 3`、摘要 `af935a5ce344`、路径 `1 格`；时间轴中旧
    v2 任务仍记录 `(4,0)`，但不会覆盖当前 v1 的只读终点；再切回 v2 恢复 `(4,0)`。
  - 强制地图详情接口返回 500 时，坐标显示 `-- / --`、错误显示
    `forced map load failure`，预检和开始按钮均禁用；解除故障后可恢复。
  - 急停始终可见。一次预检得到 `timeout waiting for ready`，原因是该隔离验收没有
    启动 Webots 后端；这只验证页面和请求合同，不作为仿真通过证据。
- 完整回归：
  - `compileall` 通过；
  - Python `347 passed in 5.52s`；
  - `api.js/state.js/render.js/controls.js/replay.js/maze_editor.js` 全部通过
    `node --check`；
  - ESP32 PlatformIO 构建通过，RAM `22744/327680` bytes，Flash
    `317041/1310720` bytes。
- 边界：本 Task 未修改地图规划算法、Webots 控制器、ESP32 固件或正式站点；
  尚未证明小车可沿合法路线到达终点。

---

## 阶段 B：合法路线与墙体证据

### Task 4：分离计划墙体与实时观测墙体

**文件：**

- Modify: `rdk_maze_tuner/core/maze_map.py`
- Modify: `rdk_maze_tuner/core/maze_planner.py`
- Create: `rdk_maze_tuner/core/map_sensor_conflict.py`
- Create: `rdk_maze_tuner/tests/test_maze_map.py`
- Create: `rdk_maze_tuner/tests/test_maze_planner.py`
- Create: `rdk_maze_tuner/tests/test_map_sensor_conflict.py`

#### 4.1 写失败测试

覆盖：

- `MazeMap.from_definition` 把地图墙体保存为 `planned_walls`。
- `observe` 只写 `observed_walls`，不能覆盖计划墙体。
- 目标规划优先使用计划墙体。
- 旧 DFS 无计划地图时仍可使用观测墙体。
- 计划开放而前向 ToF 连续 1–2 个有效样本为墙时只积累证据。
- 连续第 3 个有效样本才触发 `MAP_SENSOR_CONFLICT`。
- 无效、超量程和不同方向样本不错误累计。
- 冲突复位只发生在任务重置，不跨 run 泄漏。

Run：

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_maze_map.py \
  rdk_maze_tuner/tests/test_maze_planner.py \
  rdk_maze_tuner/tests/test_map_sensor_conflict.py -q
```

Expected RED：当前 `observe` 直接调用 `set_wall`，会改变已加载地图墙体。

#### 4.2 最小实现

- `Cell` 明确保存 `planned_walls` 和 `observed_walls`。
- 提供 `wall_for_planning(coord, direction)`，计划值存在时不可被观测覆盖。
- 保留 `walls` 的只读兼容快照，逐步迁移现有渲染和测试。
- `MapSensorConflictDetector` 是纯状态组件，不发送动作、不修改地图。

#### 4.3 验证与提交

运行目标测试和固定完整回归。

Commit：

```text
refactor: separate planned and observed maze walls
```

**Task 4 实施记录（2026-08-02）：**

- RED：
  - 三文件目标命令在收集阶段得到 `1 error`，明确缺少
    `rdk_maze_tuner.core.map_sensor_conflict`。
  - 先排除该缺失模块运行地图与规划测试，得到 `4 failed, 1 passed in 0.03s`；
    失败均来自 `Cell` 尚无 `planned_walls/observed_walls`，并证明旧实现只有一个
    可被 `observe` 改写的墙体层。
- 最小实现：
  - `Cell` 分开保存权威 `planned_walls` 与实时 `observed_walls`；兼容属性
    `walls` 返回计划值优先的只读快照。
  - `MazeMap.from_definition` 只初始化计划层，`observe/set_wall` 只写观测层；
    `wall_for_planning` 在存在计划值时拒绝观测覆盖，无计划地图则回退到观测值。
  - 地图快照同时导出兼容墙体、计划墙体和观测墙体，便于后续诊断与回放。
  - 旧 DFS 改用 `wall_for_planning`，所以不可穿越计划墙，同时保留无计划地图的
    观测式探索行为。
  - 新增纯状态 `MapSensorConflictDetector`：只在计划开放方向连续 3 个有效墙样本
    后锁存 `MAP_SENSOR_CONFLICT`；无效、超量程、开放读数、不同方向或计划墙读数
    会打断未完成证据，但已锁存冲突只能通过显式 `reset(run_id=...)` 清除。
- 目标测试：`8 passed in 0.02s`。
- 完整回归：
  - `compileall` 通过；
  - Python `355 passed in 5.66s`；
  - `api.js/state.js/render.js/controls.js/replay.js` 全部通过 `node --check`；
  - ESP32 PlatformIO 构建通过，RAM `22744/327680` bytes，Flash
    `317041/1310720` bytes。
- 边界：本 Task 只建立地图证据分层和纯冲突检测组件；冲突检测器尚未接入任务
  运行器，因此还不会自动发送 `stop` 或把任务置为 `ERROR`。未修改 Webots、
  ESP32 固件或正式站点，也未执行仿真和真车验收。

### Task 5：实现确定性地图终点规划器

**文件：**

- Create: `rdk_maze_tuner/core/goal_directed_planner.py`
- Create: `rdk_maze_tuner/tests/test_goal_directed_planner.py`
- Modify: `rdk_maze_tuner/dashboard/app.py`
- Modify: `rdk_maze_tuner/tests/test_task_orchestrator.py`
- Create: `rdk_maze_tuner/tests/test_goal_navigation_integration.py`

#### 5.1 写失败测试

覆盖：

- 从 `(0,4)·N` 到 `(4,0)` 生成合法路线。
- 路线包含所需转向和逐格前进，不穿计划墙体、不越界。
- 固定地图和方向优先级生成完全一致的动作序列。
- 到达终点时不再产生动作。
- 当前可靠位置改变后重新规划，而不是继续使用失效队列。
- 无路径时返回明确 `NO_PATH`。
- `run_kind=auto_to_map_goal` 使用新规划器。
- `run_kind=exploration_complete` 仍使用 DFS，不改变旧建图模式。

Run：

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_goal_directed_planner.py \
  rdk_maze_tuner/tests/test_goal_navigation_integration.py \
  rdk_maze_tuner/tests/test_task_orchestrator.py -q
```

Expected RED：当前只有 DFS `MazePlanner`。

#### 5.2 最小实现

- BFS 节点为格子，边来自 `wall_for_planning`。
- 终点 tie-break 与 `MapGoalResolver` 共用固定 `(y, x)` 规则。
- 格子路径转换为 `turn_left/right/back` 和 `move_cell`。
- 规划器只读地图和可靠位姿，不访问适配器、串口、网页或真值。
- `runner_factory` 按 `run_kind` 注入规划器。
- 发出不可变 `route.planned` 事件，保存格子路径和动作摘要。

#### 5.3 验证与提交

运行目标测试和固定完整回归。

Commit：

```text
feat: plan deterministic routes to map goals
```

**Task 5 实施记录（2026-08-02）：**

- RED：目标命令在收集阶段得到 `2 errors`，两组新测试均明确缺少
  `rdk_maze_tuner.core.goal_directed_planner`；现有系统只有 DFS。
- 最小实现：
  - 新增 `GoalDirectedPlanner`，每个动作边界都从当前可靠格子和车头重新执行 BFS，
    只接受 `wall_for_planning(...) is False` 的边，并用固定方向优先级保证可复现。
  - 多候选终点先按最短格数，再按 `(y,x)` 决胜；格子路线被确定性转换为
    `turn_left/right/back` 和带全局方向的 `move_cell` 动作。
  - `GoalRoute` 使用冻结数据类和元组保存地图版本、摘要、起点、车头、终点、格子
    路径与动作摘要；无合法路径使用稳定错误码 `NO_PATH`，供 runner 以
    `exhausted` 进入现有任务错误流程。
  - `MazeRunner` 只通过规划器协议取下一动作；目标规划器产生的新路线在
    `planned_action` 前发出 `route.planned`，由编排器写入不可变事件存储。
  - Dashboard `runner_factory` 按 `run_kind` 注入：`auto_to_map_goal` 使用冻结的
    任务主终点创建 `GoalDirectedPlanner`，`exploration_complete` 继续使用旧
    `MazePlanner` DFS。
- 目标测试：`24 passed in 1.02s`。其中 `(0,4)·N -> (4,0)` fixture 的首个北向
  边被计划墙封闭，实际路线为
  `(0,4)->(1,4)->(1,3)->(1,2)->(1,1)->(1,0)->(2,0)->(3,0)->(4,0)`；
  测试逐格验证不穿墙、不越界，并验证位姿改变后不消费旧动作队列。
- 完整回归：
  - `compileall` 通过；
  - Python `364 passed in 5.68s`；
  - `api.js/state.js/render.js/controls.js/replay.js` 全部通过 `node --check`；
  - ESP32 PlatformIO 构建通过，RAM `22744/327680` bytes，Flash
    `317041/1310720` bytes。
- 边界：本 Task 证明的是软件规划与事件合同；尚未加入动作物理证据门，也未在
  Webots 或真车上执行整条路线，因此不能据此声称小车已经到达终点。未部署正式站点。

---

## 阶段 C：物理到格证据

### Task 6：冻结完成阈值并建立动作证据门

**文件：**

- Create: `rdk_maze_tuner/core/motion_evidence.py`
- Create: `rdk_maze_tuner/tests/test_motion_evidence.py`
- Modify: `rdk_maze_tuner/config/params.yaml`
- Modify: `rdk_maze_tuner/config/limits.yaml`
- Modify: `rdk_maze_tuner/core/param_manager.py`
- Modify: `rdk_maze_tuner/core/auto_tuner.py`
- Modify: `rdk_maze_tuner/platform/task_orchestrator.py`
- Modify: `rdk_maze_tuner/tests/test_param_manager.py`
- Modify: `rdk_maze_tuner/tests/test_auto_tuner.py`

#### 6.1 写失败测试

覆盖固定阈值：

```text
nominal_position_error_ratio = 0.10
recoverable_position_error_ratio = 0.20
nominal_heading_error_deg = 8
recoverable_heading_error_deg = 12
goal_min_confidence = 0.80
max_recovery_attempts_per_cell = 2
```

覆盖判定：

- 名义误差输出 `accepted`。
- `450 mm -> 365 mm` 的约 18.9% 欠行程输出 `recoverable`。
- 超过 20% 输出 `unsafe`。
- 编码器运动而外部位移接近 0 输出 `WHEEL_SLIP_DETECTED`。
- 方向误差 8°/12° 边界行为固定。
- 置信度低于 0.80 输出 `POSE_UNCERTAIN`。
- `sim_truth` 即使变化也不能改变输出。
- 配置含未知字段、非法范围和非有限数时拒绝。
- 自动调参不能提出或应用 `arrival_verification.*`。
- 任务创建时冻结完整阈值快照，运行中参数变化不影响旧任务。

Run：

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_motion_evidence.py \
  rdk_maze_tuner/tests/test_param_manager.py \
  rdk_maze_tuner/tests/test_auto_tuner.py \
  rdk_maze_tuner/tests/test_task_orchestrator.py -q
```

Expected RED：动作证据门和完成阈值尚不存在。

#### 6.2 最小实现

定义冻结类型：

```text
ArrivalVerificationConfig
MotionEvidenceInput
MotionEvidenceDecision
RecoverySuggestion
```

`MotionEvidenceGate` 是纯函数式组件，只返回：

- `accepted`
- `recoverable`
- `unsafe`

任务记录和 run metadata 保存阈值快照。阈值不导出到 ESP32 参数，不加入
`AutoTuner` allowlist。

#### 6.3 验证与提交

运行目标测试和固定完整回归。

Commit：

```text
feat: add immutable motion arrival evidence
```

**Task 6 实施记录（2026-08-02）：**

- RED：
  - 四文件目标命令在收集阶段得到 `1 error`，明确缺少
    `rdk_maze_tuner.core.motion_evidence`。
  - 排除该缺失模块后得到 `21 failed, 7 passed in 0.75s`；失败覆盖缺少阈值配置、
    非有限值仍可进入参数、自动调参未显式过滤完成判据，以及编排器没有快照 provider。
- 最小实现：
  - 新增冻结类型 `ArrivalVerificationConfig`、`MotionEvidenceInput`、
    `MotionEvidenceDecision`、`RecoverySuggestion` 和纯 `MotionEvidenceGate`。
  - 默认值精确冻结为位置 `0.10/0.20`、车头 `8°/12°`、最低置信度 `0.80`、每格
    最多修正 `2` 次；严格拒绝未知字段、缺字段、布尔冒充数字、非法关系、越界和
    `NaN/Inf`。
  - 判定顺序固定：低置信度 `POSE_UNCERTAIN`，编码器明显运动而外部位移近零
    `WHEEL_SLIP_DETECTED`，名义范围 `accepted`，可恢复范围生成有限
    `nudge_forward/align_heading`，超限或用尽次数为 `unsafe`。
  - `MotionEvidenceInput.from_mapping` 明确丢弃 `sim_truth`，相同算法证据在极端不同
    真值下生成完全相同决定。
  - 参数 YAML 增加 `arrival_verification`；`ParamManager` 在载入和原子更新后都用
    冻结类型复核，并拒绝 `source=auto_tune` 修改该命名空间。完成阈值不在
    `ESP32_EXPORTS`。
  - `AutoTuner` 增加显式 allowlist，即使某条规则错误提出完成阈值也会在提案阶段
    被过滤。
  - `TaskOrchestrator.create_task` 从 provider 复制并校验完整阈值；同一快照进入
    任务、`task.created` 和 run metadata。之后 provider/参数变化不会回写旧任务。
- 目标测试：`37 passed in 1.17s`；其中 `450 -> 365 mm` 位置误差
  `85/450 ≈ 18.89%`，稳定判为 `recoverable`。
- 完整回归：
  - `compileall` 通过；
  - Python `379 passed in 5.74s`；
  - `api.js/state.js/render.js/controls.js/replay.js` 全部通过 `node --check`；
  - ESP32 PlatformIO 构建通过，RAM `22744/327680` bytes，Flash
    `317041/1310720` bytes。
- 边界：本 Task 只提供纯证据判定与不可变配置；`MazeRunner` 仍未调用证据门，
  因而格子推进语义尚未改变，修正动作也尚未发送。未修改固件或部署正式站点，
  未执行 Webots/真车路线验收。

### Task 7：让 MazeRunner 依据融合证据推进格子

**文件：**

- Create: `rdk_maze_tuner/core/task_pose_tracker.py`
- Create: `rdk_maze_tuner/core/wall_evidence.py`
- Create: `rdk_maze_tuner/tests/test_task_pose_tracker.py`
- Modify: `rdk_maze_tuner/core/maze_runner.py`
- Modify: `rdk_maze_tuner/core/pose_fusion.py`
- Modify: `rdk_maze_tuner/platform/task_orchestrator.py`
- Modify: `rdk_maze_tuner/dashboard/app.py`
- Modify: `rdk_maze_tuner/tests/test_maze_runner.py`
- Modify: `rdk_maze_tuner/tests/test_pose_fusion.py`
- Modify: `rdk_maze_tuner/tests/test_task_orchestrator.py`

#### 7.1 写失败测试

覆盖：

- 发送动作前保存基线编码器和融合位姿。
- 只接受对应 `action_id` 的 `done/error`。
- `done` 后先更新 `TaskPoseTracker`，再调用证据门。
- `accepted` 后逻辑格只推进一次。
- `recoverable` 不推进格子，返回 `recovery_required`。
- `unsafe` 不推进格子，返回稳定错误码。
- 转向动作在方向证据通过后才更新逻辑方向。
- 地图传感器冲突时先 stop，再进入 `MAP_SENSOR_CONFLICT`。
- `sim_truth` 只进入独立 evaluation event，不进入 tracker。
- 沿运动轴的前后 ToF 墙距变化可以提供外部位移证据；没有有效纵向墙距时不能把
  编码器自身重复计算为外部证据。
- 没有 IMU 时，只有成功动作证据、稳定格方向和至少两个独立墙面约束共同成立，
  才允许融合置信度达到 0.80；否则保持降级并返回 `POSE_UNCERTAIN`。
- Dashboard 的空闲定位和任务定位使用同一核心算法，但任务完成只信任
  TaskRunner 的 run-scoped tracker。

Run：

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_task_pose_tracker.py \
  rdk_maze_tuner/tests/test_maze_runner.py \
  rdk_maze_tuner/tests/test_pose_fusion.py \
  rdk_maze_tuner/tests/test_task_orchestrator.py -q
```

Expected RED：当前 `MazeRunner` 收到 `done` 后立即
`apply_completed_action`。

#### 7.2 最小实现

- `TaskPoseTracker` 组合现有 `PoseFusion`、`SlipEstimator` 和墙面约束生成器。
- tracker 生命周期严格等于一次 run，reset 后重新创建。
- `MazeStepResult` 增加证据、可靠位姿和错误码字段。
- `MazeRunner` 只消费白名单融合字段。
- Task 8 尚未接入修正动作前，`recoverable` 安全停止为
  `MOTION_RECOVERY_REQUIRED`，确保中间提交不会继续累计误差。

#### 7.3 验证与提交

运行目标测试和固定完整回归。涉及 Webots 数据合同，隔离服务器重跑 P1–P4。

Commit：

```text
fix: gate maze progress on fused motion evidence
```

**Task 7 实施记录（2026-08-02）：**

- RED：
  - 四文件目标命令首先在收集阶段得到 `2 errors`，明确缺少
    `rdk_maze_tuner.core.task_pose_tracker`。
  - 建立基础门控后，补充“`done` 只有编码器、最终 ToF/IMU 位于新鲜
    telemetry”用例；实现前该单测为 `1 failed`，结果错误落入 `unsafe`，
    证明不能复用动作前测距。
- 最小实现：
  - 新增 run-scoped `TaskPoseTracker`，动作发送前冻结编码器、融合位姿和墙距
    基线；只接收匹配 `action_id/name` 的 `done/error`。
  - 新增共享 `WallEvidenceBuilder`；Dashboard 空闲定位和任务定位均使用同一套
    不可变地图墙坐标、ToF 约束和纵向墙距变化算法。
  - 沿运动轴仅使用动作前后匹配同一墙面的前/可选后向 ToF 差值作为外部位移；
    没有有效纵向墙距时 `external_displacement_mm=0`，不会把编码器位移重复
    当作独立证据。
  - 无 IMU 时只有动作成功、格方向稳定且同时存在 x/y 两个独立墙面约束，任务
    置信度才可达到 `0.80`；否则固定保持 `<0.80` 并进入
    `POSE_UNCERTAIN`。
  - `MazeRunner` 在动作前订阅单一 `DeviceSession` 的 telemetry，动作完成后
    等待编码器与 `done` 匹配的新鲜帧，再依次记录 `pose.updated` 和
    `motion_evidence`。只有 `accepted` 才推进并锚定逻辑格，且只推进一次。
  - `recoverable` 不推进，Task 8 接入修正前以
    `MOTION_RECOVERY_REQUIRED` 停车；`unsafe` 保留稳定错误码。地图与传感器
    冲突同样不发动作，由编排器先调用 adapter `stop`，再写
    `MAP_SENSOR_CONFLICT` 错误。
  - `sim_truth` 被融合字段白名单剔除，只产生独立 `sim.evaluation` 误差事件；
    不进入 tracker、滑移估计或动作证据。
  - 每次 reset 后由 Dashboard runner factory 新建 tracker，任务完成只信任
    TaskRunner 持有的该 run tracker；Dashboard 空闲估值不能推进任务。
- 目标测试：四文件命令最终为 `50 passed in 1.44s`；其中独立的新鲜 telemetry
  RED 用例实现后为 `1 passed`。
- 完整回归：
  - `compileall` 通过；
  - Python `395 passed in 6.06s`；
  - `api.js/state.js/render.js/controls.js/replay.js` 全部通过
    `node --check`；
  - ESP32 PlatformIO 构建通过，RAM `22744/327680` bytes，Flash
    `317041/1310720` bytes；
  - `git diff --check` 通过。
- 隔离服务器 P1–P4：
  - 首次向共享验收目录写入时因当前 SSH 用户无目录写权限，在创建临时目录前
    失败，未启动 Webots；未扩大目录权限。
  - 改用用户自有隔离目录后，run
    `physical-20260802T125924Z-d6af5716` 顶层为 `PASS`，Webots
    `R2025a`；P1 水平漂移 `0.0 m`、最大倾角 `0.004738°`，P2 三向 ToF
    最大误差与固定种子扩散均为 `0.0 mm`，控制周期 `8.0 ms`，实时倍率
    `0.9473768807171323`。
  - 报告归档为
    `/home/ubuntu/maze-acceptance/task7/physical-20260802T125924Z-d6af5716/report.json`，
    SHA-256 为
    `6b1a13236ff30287c01f8128b2bb38d5d070528e4414053be00815822a587711`。
    该部署 release 不含 Git 元数据，报告 `source_commit=unknown`；因此本条
    证明现有 P1–P4 物理链和数据合同未回归，不冒充本地未提交源码的发布验收。
- 边界：未修改 ESP32 固件动作实现，未烧录、连接或驱动真实小车；Task 8 的有限
  修正动作尚未实现，P5 完整走到 `(4,0)` 和正式站点发布也仍未执行。

### Task 8：实现有限修正动作的统一协议

**文件：**

- Modify: `rdk_maze_tuner/core/protocol.py`
- Modify: `rdk_maze_tuner/core/device_session.py`
- Modify: `rdk_maze_tuner/core/serial_client.py`
- Modify: `rdk_maze_tuner/core/motion_targets.py`
- Modify: `rdk_maze_tuner/core/maze_runner.py`
- Modify: `rdk_maze_tuner/tests/test_protocol.py`
- Modify: `rdk_maze_tuner/tests/test_device_session.py`
- Modify: `rdk_maze_tuner/tests/test_motion_targets.py`
- Modify: `rdk_maze_tuner/tests/test_maze_runner.py`
- Modify: `simulation/webots/maze_car/controllers/maze_sim_controller/sim_engine.py`
- Modify: `simulation/webots/maze_car/controllers/maze_physical_controller/action_controller.py`
- Modify: `simulation/webots/maze_car/controllers/maze_physical_controller/physical_engine.py`
- Modify: `rdk_maze_tuner/tests/test_webots_sim_bridge.py`
- Modify: `rdk_maze_tuner/tests/test_physical_action_controller.py`
- Modify: `esp32_firmware/include/protocol.h`
- Modify: `esp32_firmware/include/motion_controller.h`
- Modify: `esp32_firmware/src/protocol.cpp`
- Modify: `esp32_firmware/src/motion_controller.cpp`

#### 8.1 写失败测试

统一动作：

```json
{
  "type": "action",
  "action_id": "recovery-...",
  "name": "nudge_forward",
  "speed": 0.10,
  "target_ticks": 300,
  "recovery": true
}
```

```json
{
  "type": "action",
  "action_id": "recovery-...",
  "name": "align_heading",
  "direction": "left",
  "speed": 0.09,
  "target_ticks": 60,
  "recovery": true
}
```

覆盖：

- `nudge_forward` 不超过 0.25 格。
- `align_heading` 只接受 `left/right`，角度不超过 15°。
- 修正速度不超过当前基础速度的 50%。
- 每格最多两次修正。
- 修正动作等待自己的 `ack` 和匹配 `done/error`。
- 正常动作和修正动作不能复用 `action_id`。
- 仿真确定性引擎、物理引擎和 ESP32 使用相同名称与方向语义。
- ESP32 本地再次限幅，非法修正返回 error 并停车。
- 修正成功后重新计算证据；只在通过后推进原计划格一次。
- 两次失败后进入 `MOTION_RECOVERY_FAILED`。

Run：

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_protocol.py \
  rdk_maze_tuner/tests/test_device_session.py \
  rdk_maze_tuner/tests/test_motion_targets.py \
  rdk_maze_tuner/tests/test_maze_runner.py \
  rdk_maze_tuner/tests/test_webots_sim_bridge.py \
  rdk_maze_tuner/tests/test_physical_action_controller.py -q
```

Expected RED：当前动作协议只支持 `move_cell` 和固定转向动作。

#### 8.2 最小实现

- 扩展 `build_action` 的可选恢复字段，不改变换行 JSON 和单 reader。
- 修正仍由 ESP32/模拟 ESP32 的本地非阻塞状态机执行。
- `nudge_forward` 复用直行闭环但使用受限目标。
- `align_heading` 复用转向闭环，方向必须显式。
- `MazeRunner` 管理每格修正计数和原动作关联。
- 所有修正事件包含原始 `action_id` 和修正 `action_id`。

#### 8.3 验证与提交

运行目标测试、固定完整回归和隔离服务器 P1–P4。没有新鲜真车确认时只执行
PlatformIO 编译，不上传固件。

Commit：

```text
feat: add bounded motion recovery actions
```

实施记录：

- RED：
  - 新增统一恢复协议、修正目标、单 reader 路由、每格两次上限、确定性仿真和
    物理控制器本地限幅测试。
  - 首次目标测试为 `10 failed, 72 passed`；失败集中在现有
    `build_action` 不接受恢复字段、目标解析器没有恢复目标、Runner 仍返回
    `recovery_required`，以及两个仿真执行端不认识恢复动作。
- 实现：
  - `build_action`、`SerialClient` 和 `DeviceSession` 支持可选
    `recovery/direction/parent_action_id`，正常动作帧保持原样；会话内已使用的
    `action_id` 不得再次发送，结果仍由单一 reader 按自己的 ID 路由。
  - `MotionTargetResolver` 将 `nudge_forward` 限制为当前格长的 25%，将
    `align_heading` 限制为 `left/right` 和 15°，速度分别不超过当前直行/转向
    速度的 50%。
  - `MazeRunner` 为每次修正生成独立 ID，最多执行两次；`TaskPoseTracker`
    始终从原动作基线重算累计位移、航向与墙面证据。只有新证据通过后才把原计划
    动作提交一次；耗尽上限返回 `MOTION_RECOVERY_FAILED`。
  - 确定性引擎、Webots 物理控制器和 ESP32 固件统一实现
    `nudge_forward/align_heading` 名称与方向语义，并在电机启动前再次检查
    距离、角度和速度上限。ESP32 非法命令保持停车。
- 验证：
  - 目标测试：`82 passed`。
  - 完整 Python 回归：`405 passed`；`compileall`、五个 Dashboard JavaScript
    语法检查和 `git diff --check` 均通过。
  - PlatformIO 仅编译通过，RAM `22800 / 327680`（7.0%），Flash
    `319257 / 1310720`（24.4%）；未上传、未连接或驱动真实小车。
  - 隔离服务器 P1–P4：`physical-20260802T132207Z-8c3ddb89` 为 PASS，
    Webots `R2025a`；P1 水平漂移 `0.0 m`、最大倾角 `0.004738°`，P2 三向
    ToF 最大误差和重复离散均为 `0.0 mm`，四个 P3/P4 场景实时倍率为
    `0.9456516166248197`–`0.9530780321445567`。
  - 报告归档为
    `/home/ubuntu/maze-acceptance/task8/physical-20260802T132207Z-8c3ddb89/report.json`，
    SHA-256 为
    `8ac051eda279febb2b47bb5bd91d1ff9adf4bfede8c06c9dea41ce0dd2222020`。
    验收使用隔离复制的当前工作树并排除 `.git`，因此报告
    `source_commit=unknown`；这证明 Task 8 源码的 P1–P4 物理链未回归，
    不冒充正式发布或 P5 终点验收。

---

## 阶段 D：可靠完成、调试和回放

### Task 9：建立可靠终点验证、事件和历史回放兼容

**文件：**

- Create: `rdk_maze_tuner/core/goal_verifier.py`
- Create: `rdk_maze_tuner/tests/test_goal_verifier.py`
- Modify: `rdk_maze_tuner/core/maze_runner.py`
- Modify: `rdk_maze_tuner/platform/task_orchestrator.py`
- Modify: `rdk_maze_tuner/platform/replay.py`
- Modify: `rdk_maze_tuner/platform/scoring.py`
- Modify: `rdk_maze_tuner/dashboard/static/replay.js`
- Modify: `rdk_maze_tuner/tests/test_maze_runner.py`
- Modify: `rdk_maze_tuner/tests/test_task_orchestrator.py`
- Modify: `rdk_maze_tuner/tests/test_replay.py`
- Modify: `rdk_maze_tuner/tests/test_scoring.py`

#### 9.1 写失败测试

`GoalVerifier` 必须同时验证：

- 任务终点来源地图版本和摘要匹配。
- 逻辑格为主终点。
- 最近动作有匹配成功 `done`。
- 可靠融合格为主终点。
- 连续位姿在终点容差内。
- 置信度不低于冻结阈值。
- 没有未处理的冲突、急停、断联或动作错误。

事件覆盖：

- 新运行只产生 `step.goal_verified`。
- `TaskOrchestrator` 只在 `goal_verified` 后 finalizing 和 completed。
- `task.completed.reason` 仍为 `goal_reached`，保持评分合同。
- 历史 `step.goal_reached` 在回放中标为“旧版逻辑到达”，不伪造物理证据。
- 路线、动作、融合位姿、修正次数、参数快照和地图摘要进入结构化回放。
- 未验证到达不能获得完成分。

Run：

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_goal_verifier.py \
  rdk_maze_tuner/tests/test_maze_runner.py \
  rdk_maze_tuner/tests/test_task_orchestrator.py \
  rdk_maze_tuner/tests/test_replay.py \
  rdk_maze_tuner/tests/test_scoring.py -q
```

Expected RED：当前 `step.goal_reached` 只依赖逻辑坐标。

#### 9.2 最小实现

- `GoalVerifier` 是纯组件，不改变任务状态。
- `MazeRunner` 产生 `goal_verified` outcome 和完整证据。
- `TaskOrchestrator` 只处理新 outcome。
- replay 读取旧事件时增加 `legacy_logical_only=true`。
- scoring 只接受新验证事件；读取历史已完成 run 时保留原始分数，不重写历史。

#### 9.3 验证与提交

运行目标测试、浏览器结构化回放检查和固定完整回归。

Commit：

```text
feat: verify physical arrival before task completion
```

实施记录（2026-08-02）：

- RED 先证明 `GoalVerifier` 尚不存在；补入纯组件后，集成测试仍以
  4 failed / 54 passed 暴露旧 `goal_reached`、编排器误完成、旧回放无警示和
  未验证到达得分四条缺口。
- 新增 `GoalVerifier`，同时校验地图版本/摘要、逻辑格、匹配成功
  `done`、可靠融合格、连续位姿容差、冻结置信度以及未处理故障；生产自动任务
  由任务快照构造验证器。
- `MazeRunner` 只在原动作或受限修正动作有匹配完成证据后产生
  `step.goal_verified`；失败写 `step.goal_unverified` 和完整原因。
  `TaskOrchestrator` 只接受 `goal_verified`，但最终完成原因继续写
  `goal_reached` 以保持既有评分合同。
- replay 为旧 `step.goal_reached` 增加
  `legacy_logical_only=true` 和“旧版逻辑到达”标签；结构化轨道加入路线、
  动作、融合位姿、修正、参数快照和地图身份，页面同时显示六条对应证据轨。
  scoring 对缺少 `step.goal_verified` 的新完成事件不给完成分，不重写历史
  已冻结分数。
- 目标测试为 59 passed；含回放 UI 合同检查为 70 passed。固定完整回归为
  419 passed；PlatformIO 构建成功（RAM 22,800 / 327,680，Flash
  319,257 / 1,310,720）；五个固定 JavaScript 文件 `node --check`
  与 `git diff --check` 均通过。真实浏览器生产验收留在 Task 12 发布后执行，
  当前不拿尚未部署的旧站点冒充新版本。

### Task 10：接入隔离的手工目标单步调试

**文件：**

- Create: `rdk_maze_tuner/dashboard/routes/debug.py`
- Modify: `rdk_maze_tuner/dashboard/app.py`
- Modify: `rdk_maze_tuner/dashboard/static/api.js`
- Modify: `rdk_maze_tuner/dashboard/static/controls.js`
- Modify: `rdk_maze_tuner/dashboard/static/render.js`
- Modify: `rdk_maze_tuner/dashboard/templates/index.html`
- Create: `rdk_maze_tuner/tests/test_debug_step.py`
- Modify: `rdk_maze_tuner/tests/test_dashboard_ui.py`

#### 10.1 写失败测试

覆盖：

- 调试坐标只存在“单步调试”区域。
- 调试 API 需要登录、CSRF 和控制权租约。
- 输入必须在当前地图范围内且可达。
- API 只预览或执行一个动作，不启动后台自动任务。
- 每次执行仍等待 `action_id` 的 `done/error`。
- 调试动作不能写 `step.goal_verified`、`task.completed` 或自动任务成绩。
- 运行中的自动任务禁止调试动作。
- 观察者不能调试，但所有登录用户仍可急停。

Run：

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_debug_step.py \
  rdk_maze_tuner/tests/test_dashboard_ui.py -q
```

Expected RED：调试坐标尚无隔离后端合同。

#### 10.2 最小实现

- `POST /api/debug/step` 使用当前可靠格和调试目标计算下一动作。
- 默认先返回动作预览；操作者明确点击“执行这一步”才发送。
- 调试使用独立事件类型 `debug.*` 和独立 `action_id` 前缀。
- 调试执行复用 `MotionEvidenceGate`，但不复用自动完成判定器。

#### 10.3 浏览器验收与提交

验证自动面板始终锁定地图终点；调试坐标变化不改变自动任务请求或终点显示。

运行目标测试和固定完整回归。

Commit：

```text
feat: isolate manual targets in single-step debug
```

实施记录（2026-08-02）：

- RED 以 5 failed / 11 passed 证明 `/api/debug/step` 尚不存在；测试同时
  暴露无权限门禁、无预览合同、无单动作执行和无自动任务隔离。
- 新增租约保护的单步调试路由。请求从选定不可变地图和当前可靠格重新规划，
  拒绝非整格、越界、不可达和自动任务占有控制权；预览只写
  `debug.preview`，不发送动作。
- 明确执行时只运行路线中的第一个动作，使用独立 `debug-*` action_id，
  等待匹配 `done/error`，复用 `TaskPoseTracker` 内的
  `MotionEvidenceGate`；本路径关闭自动修正，即使证据为 recoverable 也只发
  一个动作且不推进格。接受后同步 Dashboard 可靠格和连续位姿。
- 调试事件全部使用 `debug.*`，不创建 task/run，不写
  `step.goal_verified`、`task.completed`、score 或自动任务事件。观察者被
  拒绝，登录用户的共享急停保持可用。
- 前端采用两次确认：第一次“预览下一动作”，目标和地图未变化时第二次才
  “执行这一步”；任何调试坐标或地图变化都会取消确认。自动任务定义仍只读取
  地图版本，不读取调试坐标。
- Playwright 在独立本地端口完成真实浏览器验收：自动终点保持只读
  `(0,0)`；调试 `(0,1)` 产生 `debug.preview` 并显示
  “执行这一步：turn_back”；修改调试坐标后按钮恢复“预览下一动作”，自动
  终点未改变。控制台仅有登录前预期 401 和未启动 Webots 流的连接错误。
- 目标测试 18 passed；固定完整回归 426 passed；PlatformIO 构建成功
  （RAM 22,800 / 327,680，Flash 319,257 / 1,310,720）；五个固定
  JavaScript 文件 `node --check` 与 `git diff --check` 均通过。浏览器临时
  数据和 Playwright 产物已移入废纸篓，可恢复，未进入仓库。

---

## 阶段 E：真实模式同构闭环

### Task 11：复用共享核心接入 RDK Agent 真实模式

本 Task 同时关闭
`docs/superpowers/plans/2026-08-01-dual-mode-maze-control-platform.md`
中尚未完成的 Task 9–10。不得另写一套地图、规划、融合或串口 reader。

**文件：**

- Create: `rdk_maze_tuner/agent/__init__.py`
- Create: `rdk_maze_tuner/agent/config.py`
- Create: `rdk_maze_tuner/agent/client.py`
- Create: `rdk_maze_tuner/agent/runtime.py`
- Create: `rdk_maze_tuner/agent/main.py`
- Create: `rdk_maze_tuner/dashboard/routes/agents.py`
- Create: `rdk_maze_tuner/platform/device_tokens.py`
- Create: `rdk_maze_tuner/platform/param_version_repository.py`
- Create: `rdk_maze_tuner/tests/test_agent_protocol.py`
- Create: `rdk_maze_tuner/tests/test_agent_runtime.py`
- Create: `rdk_maze_tuner/tests/test_device_tokens.py`
- Create: `rdk_maze_tuner/tests/test_param_versions.py`
- Create: `deploy/rdk/maze-agent.service`
- Create: `deploy/rdk/install_agent.sh`
- Create: `deploy/rdk/maze-agent.env.example`
- Modify: `rdk_maze_tuner/platform/modes/real.py`
- Modify: `rdk_maze_tuner/dashboard/app.py`
- Modify: `requirements.txt`
- Modify: `docs/superpowers/plans/2026-08-01-dual-mode-maze-control-platform.md`

#### 11.1 写失败测试

覆盖：

- 设备令牌只保存摘要，绑定 device ID，可吊销和轮换。
- RDK 主动使用验证系统 CA 和域名的 WSS 连接，不提供关闭 TLS 校验的选项。
- 服务器任务信封包含地图版本、地图摘要、主终点、参数版本、参数快照和完成阈值。
- Agent 加载地图后再次核对摘要和终点，任何不一致都拒绝开始。
- Agent 本地复用 `DeviceSession`、`GoalDirectedPlanner`、`TaskPoseTracker`、
  `MotionEvidenceGate`、`GoalVerifier` 和有限修正动作。
- fake serial 能从 `(0,4)·N` 运行到 `(4,0)`，服务器只接收任务级状态和事件。
- 服务器不发送左右轮 PWM，不参与逐动作高频闭环。
- 断云时 Agent 立即 stop，确认本地停车后进入 LOST，不自动续跑。
- 断串口、动作超时、前方过近和急停均由本地链优先处理。
- 参数版本不可变；安全参数和完成阈值不接受自动修改。
- `RealModeAdapter` 只有在经过认证的 Agent 在线时才通过预检。
- Agent 离线时继续明确返回 `DEVICE_OFFLINE`。

Run：

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_agent_protocol.py \
  rdk_maze_tuner/tests/test_agent_runtime.py \
  rdk_maze_tuner/tests/test_device_tokens.py \
  rdk_maze_tuner/tests/test_param_versions.py -q
```

Expected RED：当前 `RealModeAdapter` 仍是离线占位，Agent 和不可变参数仓库尚不存在。

#### 11.2 最小实现

- 先完成不可变参数版本和安全域，再接入 Agent。
- Agent 只主动出站连接网站 WSS。
- 设备令牌和网站 session 完全分离。
- 串口原始读取仍由一个 `DeviceSession` 独占。
- 规划、定位、证据门和修正在 RDK 本地运行。
- 服务器只保存镜像状态、事件、成绩和回放，不把公网延迟放进动作闭环。
- 设备路径和令牌仅来自权限为 0600 的 RDK 环境文件，不进入仓库。

#### 11.3 无硬件双模式验收

使用本地 WSS、Fake Agent 和 fake serial 验证：

1. 网站切换真实模式。
2. 取得控制权。
3. 下发自动到地图终点任务。
4. RDK 本地完成规划、动作、证据和修正。
5. 网站显示位置、方向、置信度、事件和完成结果。
6. 断云和断串口均安全停车。

#### 11.4 物理硬件门

只有用户重新确认以下现场事实后，才执行真车：

- RDK X3 当前系统、网络和时间同步。
- ESP32 串口设备路径。
- 三向 ToF、编码器、电机方向。
- 实际 IMU 模块、地址和接线；没有 IMU 时保持降级合同。

未经确认不烧录。现场仍按 `AGENTS.md` 的硬件验证顺序执行。

#### 11.5 验证与提交

运行目标测试和固定完整回归；将原双模式计划 Task 9–10 的检查框和去敏实施证据
同步更新。

Commit：

```text
feat: run map-goal navigation on authenticated rdk agent
```

实施记录在执行 Task 11 时写入本节。

---

## 阶段 F：P5 与生产发布

### Task 12：建立完整到终点 P5 自动验收

**文件：**

- Create: `simulation/webots/maze_car/config/maps/task12-public-v2.json`
- Create: `simulation/webots/maze_car/config/goal_acceptance.yaml`
- Create: `simulation/webots/maze_car/tools/goal_acceptance_schema.py`
- Create: `simulation/webots/maze_car/tools/run_goal_acceptance.py`
- Create: `rdk_maze_tuner/tests/test_goal_acceptance_runner.py`
- Modify: `deploy/server/deploy_release.sh`
- Modify: `simulation/webots/maze_car/README.md`
- Modify: `docs/superpowers/plans/2026-08-02-map-goal-navigation.md`

#### 12.1 先取得并核对地图资产

从当前受控 `MapRepository` 导出 v2 的结构化定义：

- 不手工猜测内部墙体。
- 删除数据库 ID、账户和运行信息，只保存结构化地图内容。
- 本地重新运行 `validate_map_definition`。
- 计算并记录内容摘要。
- 与生产所选 v2 摘要核对；不匹配时停止。

#### 12.2 写失败测试

P5 报告必须包含：

- source commit、Webots version、地图版本和摘要。
- 参数版本、完成阈值快照、物理 profile 摘要和随机种子。
- 起点 `(0,4)·N` 和终点 `(4,0)`。
- 计划路线和动作总数。
- 每个动作的 `action_id/done/error`。
- 至少一次转向。
- 修正动作及修正前后误差。
- 最终可靠格、连续位姿、方向和置信度。
- collision count、越界数和穿墙数均为 0。
- 最终 `COMPLETED / goal_reached`。
- 结构化回放和原始 JSONL 路径。

失败用例：

- 客户端覆盖终点。
- 只到 `(0,3)`。
- 编码器空转但逻辑格推进。
- 冲突后继续运行。
- 使用 `sim_truth` 才能通过。
- 报告缺失动作、修正或地图摘要证据。

Run：

```bash
.venv/bin/python -m pytest \
  rdk_maze_tuner/tests/test_goal_acceptance_runner.py -q
```

Expected RED：P5 runner 和严格 schema 尚不存在。

#### 12.3 实现隔离 P5

- 使用临时数据目录、临时数据库和随机 loopback 端口。
- 启动一个隔离 Webots 物理进程和候选 Dashboard。
- 创建临时测试账户和控制租约，不读取生产账户密码。
- 通过真实 HTTP API 完成创建任务、预检、重置、开始、等待完成、读取成绩和回放。
- 固定地图、参数、profile 和随机种子重复运行两次。
- 任一运行未到 `(4,0)` 或出现碰撞/越界/穿墙即 FAIL。
- 报告先写临时目录，schema 验证成功后原子改名。
- `deploy_release.sh` 在原子切换前同时要求 P1–P4 和 P5 PASS。

#### 12.4 服务器验证与提交

在隔离候选服务执行：

1. 现有 P1–P4。
2. 新 P5 两次。
3. 候选 HTTPS/鉴权/租约/急停回归。
4. 完整 Python、PlatformIO 和 JavaScript 回归。

Commit：

```text
test: require complete map-goal P5 acceptance
```

实施记录必须写入实际 acceptance run_id、提交哈希和报告路径，但不能写凭据。

### Task 13：原子部署、正式浏览器验收与回滚证明

**文件：**

- Modify: `docs/superpowers/plans/2026-08-02-map-goal-navigation.md`
- 服务器生成：`/srv/maze/shared/acceptance/goal/<run_id>/`
- 服务器生成：新的不可变 release 目录

#### 13.1 发布前门禁

- 工作区无未提交的功能改动。
- Task 1–12 均有独立提交和证据记录。
- 完整本地回归通过。
- 候选服务器 P1–P5 通过。
- 上一稳定 release 和回滚命令已解析为明确路径。
- 不修改 80/443 之外的公网暴露边界。

#### 13.2 原子部署

使用现有 release 脚本创建新 release，在通过全部候选验收后原子切换
`/srv/maze/current`。不覆盖旧 release，不直接在 current 中修改文件。

#### 13.3 正式站点浏览器验收

在 `https://8.ilelezhan.cn`：

1. 登录并取得控制权。
2. 选择 `Task12 公网验收迷宫 · v2` 和 `normal-v1`。
3. 页面自动显示并锁定 `(4,0)`。
4. 预检显示 `(0,4)·N -> (4,0)`、地图摘要、路径长度和净通道结果。
5. 重置后编码器、计时、路径和任务状态归零。
6. 点击一次“开始自动探索”。
7. 观察合法路线、至少一次转向和必要修正。
8. 最终可靠格为 `(4,0)`，任务为 `COMPLETED / goal_reached`。
9. 成绩与回放包含地图摘要、参数版本、动作、定位证据和修正。
10. 急停始终可用；第二账户保持只读但可急停。

#### 13.4 故障与回滚证明

如果任一正式检查失败：

1. 立即停止任务。
2. 保留失败 run 的不可变证据。
3. 原子切回上一稳定 release。
4. 重新验证 HTTPS 健康、登录、控制权和急停。
5. 不在正式服务器直接热修。

#### 13.5 最终提交

把去敏后的 release、commit、P1–P5 run_id、正式 run_id、回滚验证和真实硬件边界
写入本计划。

Commit：

```text
docs: record map-goal production acceptance
```

## 完成定义

只有以下条件全部满足，本计划才完成：

- 自动任务无法提交或复用手工终点。
- v2 自动终点始终为 `(4,0)`。
- 路线不穿墙、不越界。
- 逻辑位置不再仅凭编码器 `done` 推进。
- 可恢复欠行程和偏航有有限动作级修正。
- 空转、低置信度和地图传感器冲突会安全停车。
- `goal_reached` 具有地图、动作、融合位姿和置信度证据。
- P1–P5、完整回归和正式浏览器验收均通过。
- 正式 release 可精确回滚。
- 仿真与真实小车验收结论分别报告。

真实小车只有在 RDK X3、ESP32、三向 ToF、编码器、电机和可选 IMU 实际在线，并按
硬件顺序完成现场验收后，才能单独标记“真实小车通过”。
