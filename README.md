# 保障灾害技术装备战备协作基础服务

本项目提供科技创新协作场景共用的服务端基础能力，用于登记科研机构、创新节点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。重大项目、科研证据、成果转化和国际合作等领域可以在这些稳定边界上扩展自己的状态、规则和接口。

在此基础之上，仓库内置了第一个领域扩展：**灾害装备战备与调拨服务**（`disaster_equipment`），把装备组件、能力参数、维护检验、操作队伍资格、运输时长、部署环境、互助协议、任务优先级和替代组合汇总为随时间变化的能力承诺，解决"纸面能力"与"真正可出动能力"不一致的问题。

## 目录

- `src/science_strategy_foundation/`：基础服务的领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/disaster_equipment/`：装备战备与调拨领域扩展（台账、能力评估、限时预留、原子出动、执行事件、越级授权、重启恢复、HTTP 路由和离线验收）；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.acceptance
PYTHONPATH=src python3 -m disaster_equipment.acceptance
```

基础服务验收在临时库中登记机构、操作者、创新节点和业务资料并核对幂等回执与审计链；装备服务验收演练"本市任务全生命周期 → 重复告警去重 → 跨区协同 → 运输途中服务重启 → 到场、收尾与归还复原"，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## 基础服务 HTTP 接口

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api --database science_strategy.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

## 灾害装备战备与调拨服务

```bash
PYTHONPATH=src python3 -m disaster_equipment.api --database disaster_equipment.sqlite3 --host 127.0.0.1 --port 8081
```

### 领域规则

- **能力承诺随时间变化**：装备可兑现与否由物理状态（在库/维修/故障停用）、占用状态（空闲/已预留/已出动）和检验证书有效期共同决定；队伍还受资格有效期约束。`GET /regions/{site}/capability` 逐台给出就绪结论与原因，直接对照纸面能力与可出动能力。
- **限时预留**：告警接入（`POST /alerts`）即按替代组合顺序评估并预留整套资源（装备 + 运输车辆 + 操作队伍），预留带到期时间；到期未出动由惰性清扫回收，任务回到候补。
- **原子核验出动**：`POST /tasks/{id}/dispatch` 在单个事务内核验整套前置条件（证书、占用、部署环境、运输路线、互助协议或越级授权），任一失败则整套预留原子释放并记录落选原因，不留部分占用；核验快照随调度单永久留存。
- **执行事件只调整未完成的承诺**：部分到场、故障（自动尝试替代组合补位）、任务延长、跨区接管、归还验收都只修改 `reserved/deployed/arrived` 状态的承诺；已执行的调度单保留原始快照，仅状态推进或标记 `superseded`。
- **重复告警不反复占用**：`alert_id` 唯一，重复告警回执既有任务；`request_id` 幂等重放首次响应。
- **并发接受单胜者**：进程锁 + `BEGIN IMMEDIATE` + 状态守卫，并发接受同一任务只有一个成功，资源竞争同理。
- **紧急越级双人确认**：缺少互助协议的跨区资源需越级授权，发起人与确认人必须不同（确认人限 admin/reviewer），授权到期自动回收尚未执行的越级承诺，已出动部分保留原记录。
- **重启恢复**：全部状态持久化在 SQLite，服务启动时自动 `recover()` 清扫到期承诺并报告在途调度，运输、交接和复原流程可继续推进。

### 主要接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /equipment`、`POST /equipment/{id}/maintenance` | 登记装备组件与检验记录 |
| `POST /teams`、`POST /routes`、`POST /agreements` | 登记操作队伍、运输时长、互助协议 |
| `POST /alerts` | 告警接入（幂等去重），自动评估并限时预留 |
| `POST /tasks/{id}/accept` | 接受候补任务（并发单胜者） |
| `POST /tasks/{id}/dispatch` | 原子核验并出动 |
| `POST /tasks/{id}/extend`、`/cancel`、`/takeover`、`/finish` | 延长、取消、跨区接管、收尾 |
| `POST /dispatches/{id}/arrival`、`/breakdown`、`/return` | （部分）到场、故障、归还验收 |
| `POST /overrides`、`POST /overrides/{id}/confirm` | 紧急越级申请与双人确认 |
| `GET /regions/{site}/capability` | 地区当前可兑现能力 |
| `GET /tasks`、`GET /tasks/{id}` | 任务与落选原因 |
| `GET /waitlist?site_id=` | 候补顺序（优先级 + 到达时间） |
| `GET /equipment?site_id=` | 单台装备就绪结论 |
| `GET /dispatches/{id}` | 调度单快照与执行事件流 |
| `POST /recovery`、`GET /health` | 人工恢复清扫、健康与审计校验 |

告警需求（`requirement`）按替代组合声明，按顺序尝试，第一套可兑现的组合胜出：

```json
{
  "environment": "urban_flood",
  "duration_hours": 6,
  "combinations": [
    {"label": "大型泵组", "items": [{"category": "pump", "min_capability": {"flow_rate_m3h": 3000}, "count": 1}], "teams": 1, "qualification": "pump_large"},
    {"label": "中型泵组合", "items": [{"category": "pump", "min_capability": {"flow_rate_m3h": 1500}, "count": 2}], "teams": 1, "qualification": "pump_large"}
  ]
}
```

运输车辆以类别 `transport_vehicle` 登记；跨区装备自动匹配运输车辆与运输路线，并计入互助协议或越级授权核验。
