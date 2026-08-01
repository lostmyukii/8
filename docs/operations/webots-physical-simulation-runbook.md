# Webots 物理仿真运行手册

## 1. 安全边界

- RDK X3/服务器负责看、想、建图、决策、调参和可视化。
- ESP32 负责电机、编码器、三路 ToF、动作闭环和断联停车。
- Webots 的 `sim_truth` 只用于评分和回放，不能进入普通控制输入。
- 任何 profile、地图、参数和 run 都应记录版本与摘要。
- 服务器验证不能替代真实小车的上电、轮向、编码器、测距和急停验收。

## 2. 选择仿真后端

确定性动作级后端用于快速协议和业务回归：

```bash
python3 -m simulation.webots.maze_car.standalone_server
```

物理后端使用：

```text
simulation/webots/maze_car/worlds/maze_physical_world.wbt
```

生产的 stream、desktop、headless systemd 服务都固定使用物理 world。
旧 `maze_world.wbt` 保留作确定性回归和精确回滚参照，不要覆盖。

## 3. 四种 profile

- `normal-v1`：日常基准与正常比赛地面。
- `low-v1`：整体低摩擦。
- `asymmetric-v1`：左右摩擦不一致。
- `local-patch-v1`：局部低摩擦区。

Dashboard 在仿真任务启动前选择 profile。任务进入运行态后 profile 锁定；
切换条件必须 reset 后新建 run。不要编辑既有 YAML 来“修正”历史成绩。

命令行临时选择：

```bash
export MAZE_PHYSICAL_PROFILE_ID=low-v1
export MAZE_PHYSICAL_PROFILE_DIR="$PWD/simulation/webots/maze_car/config/physical_profiles"
webots simulation/webots/maze_car/worlds/maze_physical_world.wbt
```

## 4. 启动和模式切换

```bash
sudo maze-sim-mode status
sudo maze-sim-mode stream
sudo maze-sim-mode desktop
sudo maze-sim-mode headless
```

- `stream`：Webots W3D，适合浏览器观看。
- `desktop`：共享 Xfce/noVNC，适合完整 Webots GUI 调试。
- `headless`：不渲染，适合自动验收。
- 三者互斥；切换后 Dashboard 会重连 `127.0.0.1:8765`。

查看服务：

```bash
systemctl status maze-webots-stream maze-dashboard caddy
journalctl -u maze-webots-stream -u maze-dashboard -f
ss -ltn
```

## 5. 网站任务操作

推荐顺序：

1. 登录。
2. 选择“仿真模式”。
3. 选择地图版本和物理 profile。
4. 取得控制权租约。
5. `preflight`，确认地图包络、摘要和设备 ready。
6. `reset`。
7. `start`。
8. 运行中可 `pause`，恢复前重新确认状态。
9. 普通结束使用 `stop`。
10. 危险、失控、空转或协议异常立即使用页面常驻“急停”。
11. 只有确认原因消除后才执行 `clear_estop`。

任意登录用户都能急停；只有租约持有人可改参数和发普通动作。急停不会
因为浏览器断开而自动清除。

## 6. 自动 P1–P4 验收

```bash
cd /srv/maze/current
.venv/bin/python \
  -m simulation.webots.maze_car.tools.run_physical_acceptance \
  --webots /usr/local/bin/webots \
  --world simulation/webots/maze_car/worlds/maze_physical_calibration.wbt \
  --scenarios simulation/webots/maze_car/config/acceptance_scenarios.yaml \
  --output /srv/maze/shared/acceptance/physical
```

只有命令返回 0 且 `report.json` 的顶层 `status` 为 `PASS` 才算通过。
`unavailable`、ready 超时、协议断开、缺字段和任一阈值失败都不是 PASS。

查看最新报告：

```bash
latest=$(
  find /srv/maze/shared/acceptance/physical \
    -mindepth 1 -maxdepth 1 -type d |
  sort |
  tail -1
)
jq . "$latest/report.json"
less "$latest/events.jsonl"
```

每个验收目录还包含场景日志和截图。不要修改已完成目录。

## 7. 成绩、JSONL 和回放

生产数据根目录：

```text
/srv/maze/shared
```

重点文件：

- SQLite：`maze-platform.sqlite3`。
- run 原始事件：`runs/<run-id>/events.jsonl`。
- 原始指标：`raw_metrics.json`。
- 回放清单：`replay.json`。
- 物理验收：`acceptance/physical/<acceptance-id>/`。

run 结束后先检查原始事件、地图/profile 摘要、seed、controller/Webots
版本，再看综合分。视频缺失时应显示“结构化回放可用”，不能补造视频或
指标。

## 8. 参数调整和版本保存

允许调节运动与判断参数，例如：

- 基础速度、转向速度。
- 左右 PID。
- 编码器目标与减速区。
- 墙面判定和位姿融合权重。

不得由自动调参静默修改：

- 急停和心跳超时。
- 前方过近硬阈值。
- 电机/力矩绝对上限。
- profile 的物理身份、摘要和固定种子。

每次应用参数都要保存旧值、新值、原因、触发动作和审批人。表现好的参数
另建版本，不覆盖历史 run 的冻结快照。

## 9. 性能调优顺序

若 RTF < 0.8 或浏览器可见帧率 < 15 FPS，按以下顺序处理：

1. 关闭或降低阴影。
2. 降低纹理尺寸。
3. 关闭抗锯齿和环境光遮蔽。
4. 降低 W3D 输出分辨率。
5. 暂停不需要的录像。

禁止降低 8 ms 控制周期换帧率。telemetry 目标保持约 20 Hz。

## 10. 发布与精确回滚

发布：

```bash
sudo /srv/maze/current/deploy/server/deploy_release.sh \
  https://github.com/lostmyukii/8.git \
  main
```

发布脚本在切软链接前运行 Python、PlatformIO、JavaScript 和 P1–P4。
切换后验证 Dashboard、stream、协议、Caddy 和本机 HTTPS vhost。任何
健康失败必须恢复前一 release。

查看 release：

```bash
readlink -f /srv/maze/current
readlink -f /srv/maze/previous
ls -1 /srv/maze/releases
```

精确回滚：

```bash
sudo /srv/maze/current/deploy/server/rollback_release.sh RELEASE_ID
```

必须写明确的 `RELEASE_ID`；不要用 `pkill`、`killall` 或模糊目录名。

## 11. 公网与账户

公网只允许：

```text
22/tcp
80/tcp
443/tcp
```

Dashboard 8000、Webots 1234/8765、VNC 5901、noVNC 6080 不加入腾讯云
安全组或 UFW 公网规则。`/simulation/*` 由 Caddy 调用
`/api/auth/authorize` 成功后才代理到 Webots。

创建生产账户必须在服务器交互输入密码，避免把密码写入命令历史：

```bash
cd /srv/maze/current
sudo -u maze env MAZE_DATA_DIR=/srv/maze/shared \
  .venv/bin/python -m rdk_maze_tuner.admin create-user
```

两名操作者各自使用独立账户，不共享网站密码。

## 12. 何时回到双模式计划 Task 9

满足以下条件后再继续 RDK Agent 和真实小车接线：

- P1–P5 物理仿真证据完整。
- 公网 HTTPS、两账户、租约、急停和回放生产验收完成。
- 新 release 可精确回滚。
- 真实设备路径、Wi-Fi 密码和密钥不进入仓库。

之后按真实硬件顺序执行：串口 ready → 三路 ToF → 编码器 → 低 PWM
轮向 → 10 cm → 一格 → 45°/90° → 动作中急停 → 2×2/3×3 迷宫。

