# 水培营养液配比求解 API

用 Python / FastAPI / SQLite 构建的水培营养液配比（mixing）求解服务。用户提交若干**浓缩液**各自的氮（N）、磷（P）、钾（K）及微量元素（Ca / Mg / S / Fe / Mn / Zn / B / Cu / Mo）浓度、可用体积与目标配方体积，服务用 **minimax 线性规划**（scipy / HiGHS）求出各浓缩液与稀释水的用量，目标是：

> **最小化各元素实际配出量相对目标量的最大相对偏差。**

求解中同时满足以下**硬约束**：

- **总量约束**：浓缩液 + 稀释水 = 目标配方体积；浓缩液合计占比可设上限。
- **单液上限**：每瓶浓缩液用量不超过其可用体积（可另设 `max_volume`）。
- **稀释水约束**：可设稀释水最小用量（强制保留稀释空间）。

当容差范围内无可行解时，会在 `violations` 中返回**具体超限元素与原因**（瓶子到顶导致供给不足、或成分配比耦合导致某元素无法压低等），并附带最优折中方案供参考。

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
```

- 服务端口：**`8000`**（映射到宿主机同端口）
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- SQLite 数据文件保存在命名卷 `hydroponic-data`（容器内 `/data/hydroponic.db`），**容器启动时自动建表初始化**。

停止 / 清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据
```

<details>
<summary>不用 Docker，本地运行</summary>

```bash
pip install -r requirements.txt
DATABASE_PATH=./hydroponic.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

</details>

## 接口一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/solve` | **试算**：仅求解，不入库 |
| POST | `/api/irrigation/plan` | **灌溉计划**：按 IANA 时区生成逐日执行批次（跨午夜 / 夏令时安全、合并相邻批次、每日容量校验） |
| POST | `/api/recipes?force=false` | **保存配方版本**：同名配方自动追加版本号 |
| GET | `/api/recipes` | 配方列表 |
| GET | `/api/recipes/{id}` | 配方详情（含最新版本号） |
| GET | `/api/recipes/{id}/versions` | 某配方的全部版本 |
| GET | `/api/recipes/{id}/versions/{v}` | 读取某版本完整内容（请求 + 结果） |
| GET | `/api/recipes/{id}/versions/{v}/export` | **导出该版本 JSON 文件** |
| GET | `/health` | 健康检查 |

### 单位约定

- 体积：**mL**；元素浓度：**mg/L**（以纯元素计，如 N、P、K、Ca、Mg…）。
- 目标量（mg）= 目标浓度 × 目标体积 ÷ 1000。
- 元素键大小写不敏感（`n`、`ca`、`FE` 都会归一化）；支持 `N/P/K/Ca/Mg/S/Fe/Mn/Zn/B/Cu/Mo`；拼错字段名（如把 `concentrations` 写成 `concentrates`）会直接返回 422 而不是被静默忽略。

## curl 示例

### 1. 试算

目标：配 10 L 工作液，N=180、P=50、K=85 mg/L；A 液富含 N/K，B 液富含 P/K；浓缩液最多占总体积 20%，至少加 7 L 稀释水，容差 5%。

下面这组数据恰好存在精确解：**A 液 1500 mL + B 液 125 mL + 水 8375 mL**。

```bash
curl -s http://localhost:8000/api/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "target_volume_ml": 10000,
    "targets": {"concentrations": {"N": 180, "P": 50, "K": 85}},
    "concentrates": [
      {"name": "A液", "available_volume": 2000,
       "concentrations": {"N": 1200, "K": 400}},
      {"name": "B液", "available_volume": 2000,
       "concentrations": {"P": 4000, "K": 2000}}
    ],
    "max_concentrate_fraction": 0.20,
    "min_water_ml": 7000,
    "tolerance": 0.05
  }'
```

成功时返回（节选）：

```json
{
  "feasible": true,
  "optimal": true,
  "max_relative_deviation": 0.0,
  "water_volume_ml": 8375.0,
  "concentrate_total_ml": 1625.0,
  "allocations": [
    {"concentrate": "A液", "volume_ml": 1500.0, "cap_ml": 2000.0},
    {"concentrate": "B液", "volume_ml": 125.0,  "cap_ml": 2000.0}
  ],
  "elements": [
    {"element": "N", "target_concentration": 180.0, "actual_concentration": 180.0,
     "target_amount_mg": 1800.0, "actual_amount_mg": 1800.0,
     "relative_deviation": 0.0, "status": "ok"},
    {"element": "P", "target_concentration": 50.0, "actual_concentration": 50.0,
     "relative_deviation": 0.0, "status": "ok"},
    {"element": "K", "target_concentration": 85.0, "actual_concentration": 85.0,
     "relative_deviation": 0.0, "status": "ok"}
  ]
}
```

### 2. 无可行解时的诊断信息

若浓缩液库存不足以达到目标（如 A 液只剩 50 mL），响应中 `feasible=false`，并给出具体元素与约束原因：

```json
{
  "feasible": false,
  "message": "无可行解：最优最大相对偏差为 96.67%，超过容差 5.00%，详见 violations。",
  "violations": [
    {
      "code": "ELEMENT_SHORTAGE",
      "element": "N",
      "actual": 60.0,
      "limit": 1710.0,
      "message": "元素 N 最多只能配出 60.000 mg，低于目标下限 1710.000 mg（目标 1800.000 mg，容差 5%）；受浓缩液可用体积/单液使用量上限约束，已到上限的浓缩液：A液。"
    }
  ]
}
```

`violations.code` 取值：

- `WATER_MIN_EXCEEDS_VOLUME` —— 稀释水下限超过目标总体积；
- `CONCENTRATE_FRACTION_CONFLICT` —— 稀释水下限与浓缩液最大占比互相冲突；
- `ELEMENT_SHORTAGE` —— 某元素即使所有相关浓缩液用到上限仍不足（附到达上限的瓶子）；
- `ELEMENT_SURPLUS` —— 满足其他元素下限时该元素必然超量（浓缩液成分耦合）；
- `ELEMENT_DEVIATION` —— 多个元素互相制约，最优折中仍超出容差；
- `TOLERANCE_UNREACHABLE` —— 兜底说明。

### 3. 保存配方版本

```bash
curl -s -X POST http://localhost:8000/api/recipes \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "叶菜通用配方",
    "description": "生菜/小白菜通用",
    "note": "v1 基线",
    "request": {
      "target_volume_ml": 10000,
      "targets": {"concentrations": {"N": 180, "P": 50, "K": 85}},
      "concentrates": [
        {"name": "A液", "available_volume": 2000,
         "concentrations": {"N": 1200, "K": 400}},
        {"name": "B液", "available_volume": 2000,
         "concentrations": {"P": 4000, "K": 2000}}
      ],
      "max_concentrate_fraction": 0.20,
      "min_water_ml": 7000,
      "tolerance": 0.05
    }
  }'
```

> 超出容差的试算默认不允许保存（HTTP 422 并返回求解结果）；确认要留档时加 `?force=true`。
> 同名配方再次保存会自动生成 `version: 2、3 …`，历史版本不可变。

### 4. 查询 / 导出 JSON

```bash
# 列出所有配方
curl -s http://localhost:8000/api/recipes

# 某配方的版本列表
curl -s http://localhost:8000/api/recipes/1/versions

# 读取第 1 版完整内容
curl -s http://localhost:8000/api/recipes/1/versions/1

# 导出为 JSON 文件（Content-Disposition attachment）
curl -s -OJ http://localhost:8000/api/recipes/1/versions/1/export
```

### 5. 灌溉计划 `POST /api/irrigation/plan`

接收 **IANA 时区、日期范围、每日若干开始时间 + 持续分钟数 + 每次目标水量、设备每日容量**，生成可执行批次：

- **UTC 锚定时长**：`duration_minutes` 按 UTC 起止差计算，批次**跨午夜**或**跨夏令时切换**仍精确持续指定分钟数；响应同时给出 UTC 起止时间与含偏移的本地显示时间。
- **夏令时安全**：春季拨快导致本地时间**不存在**（如 `America/New_York` 的 `02:30`）时顺延到首个有效时刻；秋季拨回导致本地时间**重复**时取第一次出现；均在 `sources[].dst_note` 标记并写入 `warnings`。
- **相邻批次合并**：所有批次按开始时间排序，间隔 **≤ 1 分钟**（含重叠、跨午夜）的批次合并为一个，水量相加；响应返回 `batches_before_merge` / `batches_after_merge` 以及合并前的 `sources`。
- **每日容量校验**：按批次的**本地开始日期**汇总当日需水与 `daily_capacity_ml` 比较；容量不足的日期返回明确的 `shortfall_ml` 缺口和 `capacity_ok=false`，**绝不截断**任何批次。

```bash
curl -s http://localhost:8000/api/irrigation/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "timezone": "Asia/Shanghai",
    "start_date": "2026-09-23",
    "end_date": "2026-09-24",
    "sessions": [
      {"start_time": "06:00", "duration_minutes": 30, "water_ml": 200},
      {"start_time": "23:30", "duration_minutes": 90, "water_ml": 300}
    ],
    "daily_capacity_ml": 1000
  }'
```

响应节选（注意 `23:30` 开始的批次跨午夜结束于次日 `01:00`，UTC 时间为前一天 `15:30Z`，时长仍为 90 分钟）：

```json
{
  "timezone": "Asia/Shanghai",
  "start_date": "2026-09-23",
  "end_date": "2026-09-24",
  "batches_before_merge": 4,
  "batches_after_merge": 4,
  "capacity_ok": true,
  "days": [
    {"date": "2026-09-23", "sessions": 2, "demand_ml": 500.0,
     "capacity_ml": 1000.0, "remaining_ml": 500.0,
     "shortfall_ml": 0.0, "within_capacity": true}
  ],
  "batches": [
    {"start_utc": "2026-09-22T22:00:00Z", "end_utc": "2026-09-22T22:30:00Z",
     "start_local": "2026-09-23T06:00:00+08:00",
     "end_local": "2026-09-23T06:30:00+08:00",
     "duration_minutes": 30, "water_ml": 200.0, "merged_sessions": 1,
     "sources": [{"date": "2026-09-23", "start_time": "06:00",
                  "duration_minutes": 30, "water_ml": 200.0, "dst_note": null}]},
    {"start_utc": "2026-09-23T15:30:00Z", "end_utc": "2026-09-23T17:00:00Z",
     "start_local": "2026-09-23T23:30:00+08:00",
     "end_local": "2026-09-24T01:00:00+08:00",
     "duration_minutes": 90, "water_ml": 300.0, "merged_sessions": 1,
     "sources": [{"date": "2026-09-23", "start_time": "23:30",
                  "duration_minutes": 90, "water_ml": 300.0, "dst_note": null}]}
  ],
  "warnings": []
}
```

**合并 + 容量缺口示例**（`06:00` 30 分钟与 `06:31` 10 分钟间隔 1 分钟被合并；当日需水 500 mL、容量只有 400 mL，缺口 100 mL，批次不截断）：

```bash
curl -s http://localhost:8000/api/irrigation/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "timezone": "UTC",
    "start_date": "2026-09-23",
    "end_date": "2026-09-23",
    "sessions": [
      {"start_time": "06:00", "duration_minutes": 30, "water_ml": 100},
      {"start_time": "06:31", "duration_minutes": 10, "water_ml": 50},
      {"start_time": "18:00", "duration_minutes": 60, "water_ml": 350}
    ],
    "daily_capacity_ml": 400
  }'
```

```json
{
  "batches_before_merge": 3,
  "batches_after_merge": 2,
  "capacity_ok": false,
  "days": [
    {"date": "2026-09-23", "demand_ml": 500.0, "capacity_ml": 400.0,
     "remaining_ml": 0.0, "shortfall_ml": 100.0, "within_capacity": false}
  ],
  "batches": [
    {"start_utc": "2026-09-23T06:00:00Z", "end_utc": "2026-09-23T06:41:00Z",
     "duration_minutes": 41, "water_ml": 150.0, "merged_sessions": 2,
     "sources": [{"start_time": "06:00", "water_ml": 100.0},
                 {"start_time": "06:31", "water_ml": 50.0}]},
    {"start_utc": "2026-09-23T18:00:00Z", "end_utc": "2026-09-23T19:00:00Z",
     "duration_minutes": 60, "water_ml": 350.0, "merged_sessions": 1,
     "sources": [{"start_time": "18:00", "water_ml": 350.0}]}
  ],
  "warnings": [
    "2026-09-23 需水 500 mL 超出设备每日容量 400 mL，缺口 100 mL（不截断批次，请调整计划或容量）"
  ]
}
```

**夏令时示例**：时区 `America/New_York`、日期 `2026-03-08`（春季拨快，02:00–03:00 不存在）、时段 `02:30 / 30 分钟 / 100 mL`，该批次会顺延到 `03:30 EDT`（即 `07:30Z`）执行，并返回：

```json
{
  "start_utc": "2026-03-08T07:30:00Z",
  "start_local": "2026-03-08T03:30:00-04:00",
  "sources": [{"date": "2026-03-08", "start_time": "02:30",
               "dst_note": "nonexistent_shifted_forward"}],
  "warnings": ["2026-03-08 02:30 在 America/New_York 不存在（夏令时拨快跳过），已顺延至 03:30 执行"]
}
```

秋季拨回（如 `2026-11-01 01:30` 当地出现两次）时取第一次（EDT，UTC-4），`dst_note` 为 `ambiguous_first_occurrence`。

> 时区名必须是 **IANA** 标识（`Asia/Shanghai`、`America/New_York`、`UTC`…，`GMT+8` 这类写法非法）。以下情况统一返回 **422**：非法时区、非法时间（如 `25:00`）、负的时长 / 水量 / 容量、`end_date` 早于 `start_date`、空的 `sessions`、拼写多余的字段。日期范围最多覆盖 367 天。

## 项目结构

```
.
├── app/
│   ├── main.py       # FastAPI 路由（试算 / 灌溉计划 / 保存 / 版本 / 导出）
│   ├── solver.py     # minimax LP 建模与无可行解诊断
│   ├── irrigation.py # 灌溉计划：时区展开、夏令时处理、批次合并、容量校验
│   ├── models.py     # Pydantic 请求/响应模型
│   ├── database.py   # SQLite 初始化与配方/版本存取
│   └── config.py     # 元素清单、默认容差、DB 路径
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
