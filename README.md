# DNS SOA 序列号审计 API

基于 **Python + FastAPI + SQLite** 的 DNS SOA serial 审计服务：

- 客户端提交区域名与**按观测时间排列**的 32 位 serial 列表；
- 服务端按 **RFC 1982** 模运算逐项判定 `advance`（前进）/ `regression`（倒退）/ `equal`（相等）/ `incomparable`（不可比较），并标出**可能使从服务器拒绝更新的节点**（`slave_reject_risk`）；
- 提供 **YYYYMMDDnn** 风格的下一序列号建议，保证相对当前值为有效前进；当日编号耗尽或存在半区间歧义时返回 **409**；
- 审计记录持久化于 SQLite，可按 ID 查询；
- 支持基于已有审计**即时生成序列修复预演**（不落库）：首个 serial 不变，其后各项与上一个修复后值按 RFC 1982 比较，非有效前进即替换为上一修复后值加 1（32 位无符号回绕）；
- 提供 **SOA 序列号持久化预留**：在 SQLite 写事务内按区域原子分配下一个 YYYYMMDDnn serial，并发请求不会获得重复值；以 `request_id` 为幂等键，重试安全返回首次结果；预留记录持久化，可按区域查询；
- 提供 **多权威节点共同发布规划**（`POST /api/zones/{zone}/release-plans`，仅规划不写库）：提交各权威节点的名称与当前 serial，在当日 `YYYYMMDD00`～`99` 候选区间内从小到大找出首个相对**每个节点**都是 RFC 1982 有效前进的候选值；当日区间不存在共同候选时返回 **409**，并给出各节点的 `equal`/`regression`/`incomparable` 阻断摘要。

## 快速启动（单条命令）

```bash
docker compose up --build
```

启动后在浏览器访问 **http://localhost:8000**（根路径返回服务信息；交互式 API 文档见 **http://localhost:8000/docs**，健康检查 http://localhost:8000/health）。

依赖安装与 SQLite 建表均在启动时自动完成，无需额外初始化步骤；数据通过命名卷 `dns-audit-data` 持久化。

不使用 Docker 时也可本地运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/audits` | 提交审计，返回逐项判定结果（201） |
| `GET` | `/api/audits/{id}` | 按 ID 查询审计记录（不存在返回 404） |
| `GET` | `/api/audits/{id}/repair-plan` | 基于已有审计生成序列修复预演，不落库（不存在返回 404） |
| `GET` | `/api/audits` | 审计记录列表（摘要，倒序） |
| `GET` | `/api/next-serial?current=<serial>` | YYYYMMDDnn 下一序列号建议（歧义/耗尽返回 409） |
| `POST` | `/api/zones/{zone}/serial-reservations` | 原子预留下一个 serial 并持久化（201；幂等重试返回 200；冲突返回 409） |
| `GET` | `/api/zones/{zone}/serial-reservations` | 按区域查询预留记录（预留先后升序，支持 `limit`/`offset`） |
| `POST` | `/api/zones/{zone}/release-plans` | 多权威节点共同发布规划，不写库（200；当日无共同候选返回 409） |
| `GET` | `/health` | 健康检查 |

### 提交审计

```bash
curl -s -X POST http://localhost:8000/api/audits \
  -H 'Content-Type: application/json' \
  -d '{"zone": "example.com", "serials": [2026091800, 2026091801, 2026091801, 2026091705]}'
```

响应（节选）：

```json
{
  "id": 1,
  "zone": "example.com",
  "created_at": "2026-09-18T08:00:00.000000Z",
  "items": [
    {"index": 0, "serial": 2026091800, "relation": "initial",    "delta": null,      "slave_reject_risk": false, "note": "首个观测值，作为后续比较基准"},
    {"index": 1, "serial": 2026091801, "relation": "advance",    "delta": 1,         "slave_reject_risk": false, "note": "按 RFC 1982 为有效前进，从服务器可正常接受更新"},
    {"index": 2, "serial": 2026091801, "relation": "equal",      "delta": 0,         "slave_reject_risk": false, "note": "与上一观测值相等，从服务器不会触发区域传输"},
    {"index": 3, "serial": 2026091705, "relation": "regression", "delta": -96,       "slave_reject_risk": true,  "note": "序列号倒退，从服务器将认为本端数据更旧而拒绝更新"}
  ],
  "summary": {"total": 4, "advance": 1, "equal": 1, "regression": 1, "incomparable": 0, "risk_indexes": [3]}
}
```

### 按 ID 查询

```bash
curl -s http://localhost:8000/api/audits/1
```

### 序列修复预演

对已提交审计即时生成修复方案，**结果不写入数据库**，也不修改原审计记录。首个 serial 保持不变；其后每项都与**上一个修复后值**按 RFC 1982 比较——有效前进则保留（`keep`），相等/倒退/不可比较时替换（`replace`）为上一个修复后值加 1，并按 32 位无符号整数（模 `2^32`）回绕。

```bash
curl -s http://localhost:8000/api/audits/1/repair-plan
```

```json
{
  "id": 1,
  "zone": "example.com",
  "created_at": "2026-09-18T08:00:00.000000Z",
  "items": [
    {"index": 0, "original": 2026091800, "effective": 2026091800, "action": "keep",    "reason": "首个观测值保持不变，作为修复序列的比较基准"},
    {"index": 1, "original": 2026091801, "effective": 2026091801, "action": "keep",    "reason": "相对上一修复后值为 RFC 1982 有效前进，保留原值"},
    {"index": 2, "original": 2026091801, "effective": 2026091802, "action": "replace", "reason": "与上一修复后值相等，未形成有效前进，替换为上一修复后值加 1"},
    {"index": 3, "original": 2026091705, "effective": 2026091803, "action": "replace", "reason": "相对上一修复后值倒退，从服务器将拒绝更新，替换为上一修复后值加 1"}
  ],
  "summary": {"total": 4, "replaced": 2}
}
```

- 每项返回 `original`（原始值）、`effective`（修复后生效值）、`action`（`keep`/`replace`）与 `reason`（保留或替换原因），`summary.replaced` 汇总替换数量；
- 例如 `4294967295 → 4294967295` 会替换为 `0`（回绕后仍是有效前进）；审计 ID 不存在时返回 **404**。

### 下一序列号建议

```bash
curl -s 'http://localhost:8000/api/next-serial?current=2026091807'
# {"current":2026091807,"next":2026091808,"date":"20260918","note":"建议值相对当前值为 RFC 1982 意义下的有效前进"}
```

- 当前值落后于当日基准（含 32 位回绕场景）时，建议当日的 `YYYYMMDD00`；
- 当前值落在当日区间内时，建议 `current + 1`；
- `nn` 已达 `99`（当日编号耗尽）、当前值领先于当日区间、或与当日基准相距恰好 `2^31`（半区间歧义）时返回 **409**；
- 可选参数 `today=YYYY-MM-DD` 指定建议所基于的日期（缺省为服务器 UTC 当日）。

### SOA 序列号持久化预留

`POST /api/zones/{zone}/serial-reservations` 在**单个 SQLite 写事务**（`BEGIN IMMEDIATE` 串行化写入）内完成"读取区域最新预留 → 按 YYYYMMDDnn/RFC 1982 规则分配 → 落库"，因此并发请求不会获得重复 serial。请求体：

| 字段 | 说明 |
| --- | --- |
| `current` | 调用方当前持有的最新 serial（区域首次预留时即其当前发布值） |
| `request_id` | 幂等键（同一区域内唯一）；相同值重试返回首次结果，不会再次推进 |

```bash
# 首次预留
curl -s -X POST http://localhost:8000/api/zones/example.com/serial-reservations \
  -H 'Content-Type: application/json' \
  -d '{"current": 2026091900, "request_id": "deploy-20260919-001"}'
```

```json
{
  "id": 1,
  "zone": "example.com",
  "request_id": "deploy-20260919-001",
  "current": 2026091900,
  "serial": 2026091901,
  "date": "20260919",
  "created_at": "2026-09-19T01:48:31.296258Z",
  "replayed": false
}
```

**幂等重试**：同一区域、同一 `request_id` 的重复请求返回 **200**（首次为 **201**），响应体与首次完全一致且 `replayed: true`——即使重试携带了不同的 `current`，也原样返回首次预留值，序列号不再推进。

**乐观并发控制**：`current` 必须与服务端为该区域预留的最新值一致；不一致时返回 **409**，并在响应体中给出最新值，调用方应使用该值重试：

```json
{
  "detail": {
    "reason": "stale_current",
    "message": "current 与服务端已预留的最新序列号 2026091901 不一致",
    "latest_serial": 2026091901
  }
}
```

典型的并发安全调用循环：

```bash
# 收到 409 后，用 detail.latest_serial 作为新的 current、保持/更换 request_id 重试
curl -s -X POST http://localhost:8000/api/zones/example.com/serial-reservations \
  -H 'Content-Type: application/json' \
  -d '{"current": 2026091901, "request_id": "deploy-20260919-002"}'
```

- 当日编号耗尽（`nn` 已到 99）、当前值领先当日区间、或半区间歧义时同样返回 **409**，`detail.reason` 分别为 `nn_exhausted` / `ahead_of_today` / `half_interval_ambiguous`，此时 `latest_serial` 为 `null`；
- 不同区域的预留计数互相独立，同一 `request_id` 可在不同区域分别使用；
- 区域首次预留不校验历史（无既有记录即接受），之后以已预留的最新值为准。

**按区域查询预留记录**（按预留先后升序，支持 `limit`/`offset` 分页）：

```bash
curl -s 'http://localhost:8000/api/zones/example.com/serial-reservations?limit=50&offset=0'
```

```json
[
  {"id": 1, "zone": "example.com", "request_id": "deploy-20260919-001",
   "current": 2026091900, "serial": 2026091901, "date": "20260919",
   "created_at": "2026-09-19T01:48:31.296258Z", "replayed": false}
]
```

> 预留记录写入同一个 SQLite 数据库（容器内 `/data/audits.db`，由命名卷 `dns-audit-data` 持久化），服务重启后仍然存在，`current` 校验与幂等重试在重启后继续生效。路径中区域名非法时返回 **400**，请求体校验失败返回 **422**。

### 多权威节点共同发布规划

`POST /api/zones/{zone}/release-plans` 用于多权威（multi-primary）发布前的序列号协调：提交各权威节点的名称与当前 serial，服务端在当日 `YYYYMMDD00`～`YYYYMMDD99` 候选区间内**从小到大**扫描，返回首个相对**每个**节点当前 serial 都属于 RFC 1982 有效前进（advance）的候选值。规划结果**仅用于发布预演，不写入数据库**（不会产生审计或预留记录）。

请求体：

| 字段 | 说明 |
| --- | --- |
| `nodes` | 节点列表（至少 1 个、至多 256 个）；每项含 `name`（节点名，去除首尾空白后须在请求内唯一）与 `serial`（当前 SOA serial，沿用 32 位无符号校验 `0..4294967295`） |
| `today` | 可选，规划所基于的日期（`YYYY-MM-DD`）；缺省为服务器 UTC 当日 |

```bash
curl -s -X POST http://localhost:8000/api/zones/example.com/release-plans \
  -H 'Content-Type: application/json' \
  -d '{
        "today": "2026-09-19",
        "nodes": [
          {"name": "ns1.example.com", "serial": 2026091805},
          {"name": "ns2.example.com", "serial": 2026010100}
        ]
      }'
```

```json
{
  "zone": "example.com",
  "date": "20260919",
  "candidate": 2026091900,
  "candidate_window": {"start": 2026091900, "end": 2026091999},
  "nodes": [
    {"name": "ns1.example.com", "serial": 2026091805, "relation": "advance", "forward_distance": 95},
    {"name": "ns2.example.com", "serial": 2026010100, "relation": "advance", "forward_distance": 81800}
  ],
  "note": "候选 serial 相对每个权威节点的当前 serial 均为 RFC 1982 意义下的有效前进；规划结果仅用于发布预演，不写入数据库"
}
```

- `candidate` 为候选区间（`candidate_window`）中首个对所有节点都有效的值；若较小候选对某个节点是相等/倒退/不可比较，会被自动跳过（例如节点已持有 `YYYYMMDD03` 时会从 `04` 起继续寻找）；
- `nodes[]` 与请求顺序一致，每项给出候选相对该节点的 `relation`（共同候选恒为 `advance`）与模 `2^32` 的 `forward_distance`（含 `4294967295 → 0` 这类回绕前进）；
- 当整个 `00..99` 区间都不存在共同候选时返回 **409**，`detail.blockers` 按节点列出其对全部 100 个候选值的阻断计数（`equal` / `regression` / `incomparable`，另附 `advance`，四项之和为 `checked`=100）：

```json
{
  "detail": {
    "reason": "no_common_candidate",
    "message": "当日 YYYYMMDDnn 候选区间（2026091900–2026091999）中不存在相对所有节点均为 RFC 1982 有效前进的共同候选 serial",
    "date": "20260919",
    "candidate_window": {"start": 2026091900, "end": 2026091999},
    "checked": 100,
    "blockers": [
      {"name": "ns1.example.com", "serial": 4173575618, "advance": 70, "equal": 0, "regression": 29, "incomparable": 1},
      {"name": "ns2.example.com", "serial": 2026091970, "advance": 29, "equal": 1, "regression": 70, "incomparable": 0}
    ]
  }
}
```

- 上例中 `ns1` 只接受 `00..69`（在 `70` 处与候选相距恰好 `2^31` 不可比较），而 `ns2` 已持有 `9970`（仅 `71..99` 才是前进、`70` 相等），两节点可行区间互不相交，因此需等待下一日期或先协调各节点 serial；
- 指定的 `today` 超出 32 位序列号可表达范围（如 `9999-12-31`）时返回 **409**，`detail.reason` 为 `date_out_of_range`；节点名重复/空白、`serial` 越界等请求体问题返回 **422**，路径中区域名非法返回 **400**。

## 判定规则（RFC 1982）

serial 是模 `2^32` 环形空间上的值。设前向距离 `d = (current - previous) mod 2^32`：

| 关系 | 条件 | 从服务器行为 |
| --- | --- | --- |
| `advance` | `1 ≤ d ≤ 2^31 - 1` | 接受更新 |
| `equal` | `d = 0` | 不触发传输 |
| `regression` | `2^31 + 1 ≤ d ≤ 2^32 - 1` | **拒绝更新**（风险标记） |
| `incomparable` | `d = 2^31` | 行为不确定（风险标记） |

因此 `4294967295 → 0` 属于前进（`d = 1`），而 `0 → 2147483648` 不可比较。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DNS_AUDIT_DB` | `audits.db`（容器内 `/data/audits.db`） | SQLite 数据库文件路径 |

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

## 项目结构

```
app/
  main.py      # FastAPI 应用与路由（启动时初始化数据库）
  rfc1982.py   # RFC 1982 序列号算术
  suggest.py   # YYYYMMDDnn 下一序列号建议
  repair.py    # 序列修复预演（不落库）
  release_plan.py  # 多权威节点共同发布规划（不落库）
  service.py   # 逐项判定与汇总
  db.py        # SQLite 持久层（审计记录 + serial 预留的事务化分配与查询）
  schemas.py   # 请求/响应模型与校验
tests/
  test_api.py  # API、算术与并发预留测试
```
