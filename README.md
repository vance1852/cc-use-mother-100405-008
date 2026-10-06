# 保障灾害技术装备战备协作基础服务

本项目提供科技创新协作场景共用的服务端基础能力，用于登记科研机构、创新节点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。重大项目、科研证据、成果转化和国际合作等领域可以在这些稳定边界上扩展自己的状态、规则和接口。

## 目录

- `src/science_strategy_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/disaster_readiness/`：灾害装备战备与调拨服务，在基础服务稳定边界上扩展；
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
```

验收命令会在临时 SQLite 数据库中登记科研机构、操作者、创新节点和业务资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api --database science_strategy.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

# 灾害装备战备与调拨服务

`src/disaster_readiness/` 把装备组件、能力参数、维护检验、操作队伍资格、运输时长、部署环境、互助协议、任务优先级和替代组合汇成**随时间变化的能力承诺**，解决“纸面能力与真正可出动能力不一致”的问题。

## 核心规则

- **可出动能力而非纸面能力**：装备必须启用、检验证书当前有效（区分缺失/过期/吊销）、无未结束维修、部署环境匹配；跨区调用还需当前有效的互助协议；同时必须配齐具备资格的操作队伍、可到场车辆和运输时长。任一不满足都会给出明确落选原因代码。
- **限时预留**：预警升级时按 `ttl_minutes` 预留资源并记录 `expires_at`；到期由 `run_due_processing` 自动释放。
- **原子核验**：正式出动前重新校验整套前置条件，任一不满足即拒绝出动；重复告警（同一 `alarm_key`）不会反复占用装备。
- **并发唯一胜者**：所有写入走 SQLite `BEGIN IMMEDIATE` 即时事务，资源占用落在主键唯一的锁表上，并发预留只有一个胜者，败者进入候补并携带落选原因。
- **只调整未完成承诺**：故障、部分到场、任务延长、跨区接管、归还验收都以追加事件/追加替代行的方式进行；已执行明细和原调度记录永不改写，已出动承诺不可取消。
- **紧急越级**：只能抢占**更低优先级的软预留（held）**，不能抢占已出动承诺；必须双人确认（发起人与复核人不同），并登记到期回收，到期后资源恢复给被抢占承诺。
- **候补**：按任务优先级降序、登记次序升序，在资源释放或修复后自动转正。
- **重启续接**：`GET /in-flight` 返回仍在运输、等待归还交接和待回收越级的承诺；状态全部持久化，重启后继续运输、交接和复原流程。

## 承诺状态机

```
held ──confirm_dispatch──> dispatched ──部分到场──> operating ──归还验收──> completed
  │                           │                        │
  │                           └──归还验收──────────────┘
  ├──到期/取消──> expired/cancelled（释放资源，触发候补转正）
  └──被越级抢占──> preempted ──越级到期恢复──> held
```

## 主要 HTTP 接口

写入类接口均需 `X-Actor-Id` 与幂等 `request_id`。

| 类别 | 接口 |
| --- | --- |
| 基础登记 | `POST /regions` `/equipment` `/certifications` `/maintenance` `/crews` `/vehicles` `/travel-times` `/aid-agreements` `/missions` |
| 查询 | `GET /readiness?region_id=&environment=`、`GET /missions/{id}/status`、`GET /waitlist`、`GET /feasibility?mission_id=`、`GET /in-flight` |
| 承诺流转 | `POST /reservations` `/dispatches` `/arrivals` `/faults` `/substitutes` `/extensions` `/handovers` `/returns` `/cancellations` `/overrides` `/processing/run-due` |

不可行时返回 `409`，`message`/`details.rejections` 中带结构化落选原因（如 `cert_expired`、`equipment_busy`、`agreement_missing`、`crew_not_qualified`、`travel_unknown`、`capacity_shortfall`）。

## 测试与验收

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m disaster_readiness.acceptance
PYTHONPATH=src python3 -m disaster_readiness.api --database disaster_readiness.sqlite3 --port 8081
```

离线验收用一条完整防汛剧情核对：证书过期/车辆被占用导致的纸面差异、限时预留与候补转正、出动前原子核验、部分到场、故障替代、任务延长、跨区接管、越级双人确认与到期回收、归还验收，以及服务重启后的状态与审计续接。

