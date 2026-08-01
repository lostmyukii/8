# Webots 物理迷宫小车验收清单

更新时间：2026-08-01 UTC  
验收服务器：8 vCPU / 16 GiB，AMD EPYC 9K65，Ubuntu 24.04  
Webots：R2025a  
固定随机种子：`20260801`

## 1. 当前结论

| 边界 | 状态 | 证据 |
|---|---|---|
| 源码/静态合同 | PASS | 本机 325 项 Python 测试；Python、JavaScript、shell 静态检查通过 |
| 无 Webots 单元测试 | PASS | fake device、协议、PID、地图、任务、成绩和回放均进入完整回归 |
| Webots 物理仿真 | PASS | `physical-20260801T184931Z-6b28a70a`，P1–P4 `overall PASS` |
| 网站本机/SSH 隧道链路 | PASS | Dashboard 登录页和 W3D 画面均真实打开；鉴权 API 合同测试通过 |
| 网站公网 HTTPS | BLOCKED | 主机 UFW 已放行 80/443，但外部连接和 ACME 均在到达主机前超时；腾讯云安全组待重新核对 |
| 服务器性能 | PASS | RTF 0.952、可见更新 19.99 FPS、telemetry 17.23 Hz、控制周期 8 ms |
| ESP32 构建 | PASS | RAM 6.9%，Flash 24.2% |
| 真实小车 | NOT TESTED | 尚未连接 ESP32、编码器、三路 ToF 和电机做物理验收 |

“Webots 物理仿真 PASS”不能解释为“真实小车可用”。

## 2. TDD 与独立提交证据

| Task | 初始 RED | 目标/完整回归 | 独立提交 |
|---|---|---|---|
| 1 不可变物理配置 | `physical_config` 模块不存在，测试收集失败 | 18 / 181 passed | `2d84831` |
| 2 仿真引擎合同 | `engine_contract` 模块不存在，测试收集失败 | 10 / 186 passed | `b798093` |
| 3 物理 PROTO/world | PROTO、两个物理 world、控制器不存在，5 项失败 | 5 / 191 passed | `abea77e` |
| 4 设备与真值隔离 | 设备、telemetry、真值模块和严格 allowlist 缺失 | 26 / 204 passed | `a425942` |
| 5 PID 与动作内核 | PID、电机模型、动作控制器模块不存在 | 20 / 224 passed | `345b932` |
| 6 物理引擎接线 | 物理引擎、world 配置器和真实 device 接线缺失 | 45 / 242 passed | `9200744` |
| 7 地图几何与目标 | `motion_targets`、`physical_preflight` 收集失败 | 48 / 269 passed | `c7f735f` |
| 8 摩擦与故障 | 场景仓库、结构化滑移指标、故障矩阵缺失 | 28 / 299 passed | `c46f6a5` |
| 9 run 固化 | `physical_profile_repository` 收集失败 | 43 / 308 passed | `ea46263` |
| 10 Dashboard 证据 | 物理选择、真值隔离、回放字段合同先失败 | 17 / 310 passed | `89b7014` |
| 11 自动物理验收/部署 | runner、报告 schema、服务部署合同先失败 | 11 / 322 passed；入口修复后 325 passed | `2e38004` |

Task 11 后续的独立部署修复：

- `5c5ad33`：候选 release 内执行检查。
- `eb0f7d6`：Node 18 使用模块模式做 JavaScript 静态检查。
- `6776aed`：恢复 Caddy 鉴权入口和公开健康端点。
- `d1817b3`：保留 Caddy reload 能力并固化 UFW 80/443。
- `dcc050c`：健康检查失败时真正触发精确回滚。

最新完整本机回归为 `325 passed`；最新服务器候选回归同为
`325 passed`。服务器 PlatformIO 为 RAM 22744/327680 bytes（6.9%）、
Flash 316937/1310720 bytes（24.2%）。

## 3. 四个不可变 profile

| Profile | SHA-256 摘要 | 设计含义 |
|---|---|---|
| `normal-v1` | `ab8ff43fb3947ac40f2d58c777c28b70172478152bb180cf035d334807b66e46` | 左右轮正常摩擦 |
| `low-v1` | `bebeae5a9e13357085092ccc44e5a473ca57e4e604f917ab05a53ea356b070ae` | 左右轮低摩擦 |
| `asymmetric-v1` | `fbdd8bf39f4d05475629e7cf7b2970d406dbf9d0b4666661dc80754330a1c438` | 左低、右正常 |
| `local-patch-v1` | `6c1626b38a0326e663ed98a0d39736e47c1ad97d0711db563ab983eec5d69f89` | 正常地面含局部低摩擦区 |

摘要来自实际部署报告；每个 profile 使用固定种子 `20260801`。运行时
只选择已有 profile，不允许原地修改 YAML 或 SQLite 快照。

## 4. P1 静止稳定

报告：`/srv/maze/shared/acceptance/physical/physical-20260801T184931Z-6b28a70a/report.json`

- 10.008 秒水平漂移：0 m，阈值 ≤ 0.002 m。
- 垂直沉降：0.000011242 m。
- 最大垂直速度：0.000299947 m/s，阈值 ≤ 0.05 m/s。
- 最大倾角/姿态变化：0.004738°，阈值分别 ≤ 5°/1°。
- 未倾覆、未穿地。
- P1：PASS。

## 5. P2 三向 ToF 与固定种子

- 前/左/右理想值：536 / 574 / 574 mm。
- 8 帧最大几何误差：三个方向均 0 mm，阈值 ≤ 10 mm。
- 固定种子最大重复离散：三个方向均 0 mm，阈值 0 mm。
- P2：PASS。

## 6. P3 正常动作

`normal-p3-v1`：

- 请求/完成：30/30，成功率 100%，阈值 ≥ 90%。
- 最大距离误差：11.0999 mm，阈值 ≤ 15 mm。
- 最大航向误差：0.955738°，阈值 ≤ 3°。
- 最大转向误差：1.211895°，阈值 ≤ 3°。
- 碰撞：0。
- realTimeFactor：0.95445。
- P3：PASS。

## 7. P4 摩擦差异

| 场景 | 动作 | 关键实测 | 阈值结果 |
|---|---:|---|---|
| `asymmetric-v1` | 3/3 | 左右滑移差 0.048806；航向差 8.611818° | PASS |
| `local-patch-v1` | 8/8 | 滑移增量 0.502025；地面切换 3 次 | PASS |
| `low-v1` | 3/3 | 最低平均滑移 0.619103；编码器-真值差 88.7831 mm | PASS |

normal、low、asymmetric、local_patch 使用不同接触条件并产生不同轨迹；
普通控制器没有读取 `sim_truth`。

## 8. P5 性能与浏览器

- 汇总 realTimeFactor：0.952036，阈值 ≥ 0.8。
- Webots world 渲染目标：24 FPS。
- 浏览器 W3D 已连接到生产 stream 隧道；3.002 秒内
  `#webots-clock` 出现 60 次可见更新，即 19.9867 FPS，阈值 ≥ 15 FPS。
- W3D canvas：1280×544。
- telemetry：87 帧/4.991615 秒，即 17.229 Hz。
- 控制周期：8 ms。
- 10 秒 cgroup 采样：81.99% 单核，约占 8 vCPU 整机 10.25%。
- Webots stream 内存：240.39 MiB。
- Mac→SSH 隧道→Dashboard 五次健康请求：
  103.4/234.2/146.0/146.4/144.8 ms，中位数约 146 ms。
- 画面证据：
  `docs/acceptance/evidence/task11-webots-stream.png`。
- 生产登录页证据：
  `docs/acceptance/evidence/task11-production-login.png`。
- 1440×1000、1280×800、768×1000 响应式证据：
  `docs/acceptance/evidence/task10-*.png`。

## 9. 部署、服务与回滚

当前运行 release：`/srv/maze/releases/20260801T184853Z`  
当前源码 commit：`6776aedd3449f10dbef2556fbd4b72a9274150ec`  
回滚目标：`/srv/maze/releases/20260801T183038Z`

已验证：

- `stream → desktop → stream` 切换成功。
- `maze-webots-stream`、`maze-dashboard`、`caddy` 为 active。
- desktop 时 `8765/6080/5901` 可用且 Webots stream 停止。
- stream 恢复后 `8765/1234` 可用，desktop 停止。
- `8765/8000/6080/5901` 监听回环地址。
- Webots 的 1234 由进程监听，但 systemd 网络限制和 UFW 均阻止公网直连。
- UFW 仅放行 22、80、443。

尚未关闭：

- 腾讯云公网 80/443 未到达主机，UFW 对两端口的入站计数为 0。
- Let’s Encrypt 因公网 connect timeout 暂未签发证书。
- 生产数据库用户数为 0；不能伪造“生产账户登录、租约和任务已验收”。
- `dcc050c` 尚未形成新的只读生产 release；安全组恢复后应重新部署。

## 10. 关闭 Task 12 前必须补齐

- [ ] 在腾讯云控制台确认当前实例实际绑定的安全组入站规则包含
  TCP 80、443，来源 `0.0.0.0/0`；不要开放 1234、8000、8765、5901、6080。
- [ ] 等 Caddy 取得有效证书，外部验证 `/` 与 `/api/health`。
- [ ] 由用户选择两个生产用户名和密码，使用交互式
  `python -m rdk_maze_tuner.admin create-user` 创建。
- [ ] 两个真实生产账户完成登录、租约抢占/释放、任务、急停、成绩和回放。
- [ ] 发布包含 `dcc050c` 的新只读 release，再验证一次失败精确回滚。
- [ ] 创建一个真实仿真 run，重启 systemd 后从历史列表恢复。

