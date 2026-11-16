# 园区总表累计读数 → 月度用量发布平台

把园区总表的**累计 kWh 读数 CSV** 整理成月度数仓可用的**用量明细与月合计**的
FastAPI + SQLite 网页应用，支持补传重发、整批拒收与不可变版本发布。

## 业务规则实现

| 需求 | 实现 |
| --- | --- |
| 同刻同内容重传不重复入库 | `(ts, period_no)` 唯一；累计值相同则幂等跳过并计数 |
| 同刻异内容 | 整批回滚拒收，库内读数不变，批次表留拒收记录 |
| 同周期读数倒退 | 合并库内+本批按时间排序逐对校验，发现倒退整批拒收 |
| 仅相邻同周期算差额 | 相邻两次读数 `period_no` 相同才计算 `后笔-前笔`；换表跨周期跳过、不估算 |
| 差额归属月份 | 归入**后笔读数所在 UTC 月** |
| 两月一次发布 | 页面选择相邻两个 UTC 月，一个事务生成明细 + 月合计新版本 |
| 保留旧版 | 版本与明细均不可变，历史版本永久保留并提供明细/合计 CSV 下载 |
| 迟到补传交错 | 发布在 `BEGIN IMMEDIATE` 事务内对**当前全部读数**快照计算并固化 SHA-256 状态指纹，同一版本的两个月必然来自同一读数状态；旧版内容不变 |
| 上传失败不改读数 | 整批事务，任何校验失败都 rollback |
| 发布失败不留新版本 | 版本头、明细、合计同一事务，失败 rollback（含故障注入自测） |

## CSV 格式

UTF-8 编码，三列（表头中英文均可，顺序不限）：

- UTC 时间：`utc_time` / `ts`，必须带时区，如 `2026-01-01T00:00:00Z`
- 表计周期号：`period_no`，整数
- 累计 kWh：`cumulative_kwh`，数值

示例见 `examples/sample_readings.csv`。

## 启动（Docker Compose）

```bash
docker compose up --build -d
# 打开 http://localhost:8000
```

SQLite 数据保存在命名卷 `energy_data`（容器内 `/data/energy.db`）。

## 本地开发 / 自测

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/uvicorn app.web:app --reload
```

## 页面

- `/` 概览（读数/批次/版本统计）
- `/upload` 上传 CSV、查看批次接受/拒收记录
- `/readings` 当前读数与差额预览、读数状态指纹
- `/publish` 选择相邻两个 UTC 月发布新版本
- `/versions` 历史版本（指纹是否匹配当前读数一目了然）
- `/versions/{id}` 版本明细、月合计与 CSV 下载

## 自测要点

`tests/` 覆盖：幂等重传、异内容拒收、读数倒退拒收、跨周期不估算、
月界差额归属、迟到补传同时改变两个月、补传与发布交错的版本一致性、
发布故障注入回滚（不留新版本）、旧版不可变。
