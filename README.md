# SQL 字段溯源 + 身份归并入仓（Flask + Docker Compose）

本仓库交付两个可实际运行的页面：

1. **SQL 字段溯源 · 敏感列影响分析**（首页 `/`，原有功能）：粘贴源表字段与单条 SQLite
   SELECT，逐列展示输出字段来自哪些源字段、经由哪些 CTE 列传播，并高亮敏感来源。
   只做静态语法分析，不连接数据库、不执行 SQL。
2. **身份归并入仓**（`/identity`）：收到 CRM 合号通知后，上传合号映射与分群名单，
   沿合号链把同一人的历史名单归到最终客户号，按“最终客户号 + 分群 + 业务日”去重，
   并支持查看、下载正式名单 CSV 供投放任务读取。

---

## 1. 快速启动（Docker Compose）

```bash
docker compose up --build
```

- 首页（溯源）：<http://localhost:8000>
- 身份归并入仓：<http://localhost:8000/identity>

停止：

```bash
docker compose down
```

SQLite 数据保存在命名卷 `identity-data` 中，**容器停止、重建、重启后映射和正式名单
仍然保留**。只有显式删除卷才会清空：

```bash
docker compose down -v          # 连同数据一起删除
```

容器内使用 gunicorn 启动 `server:app`（2 进程 × 4 线程），健康检查访问 `/`。

## 2. 本地直接运行（可选，需 Python 3.11 与 Flask）

```bash
pip install -r app/requirements.txt
python3 app/server.py           # 监听 8000
# 或使用脚本（自动选择 python / python3）：
./run-local.sh
```

核心溯源与归并逻辑只依赖 Python 标准库：

```bash
python -m unittest discover -s tests -v
```

本地数据库默认写入 `app/data/identity.db`，可用 `IDENTITY_DB_PATH` 指定其他路径。

---

## 3. 身份归并入仓

### 3.1 业务规则

- **直接映射**：CRM 合号 CSV 每行是 `旧号 → 保留号`。每个旧号最多只有一个直接去向。
- **链路传递**：查询、迁移、导入时都沿着映射链走到**最终客户号**。例如
  `A→B`、`B→C`，则 A 的最终号是 C。
- **同向重传幂等**：再次上传 `A→B` 不改变映射和名单。
- **改向拒绝**：若 A 已直接指向 B，再上传 `A→C`（即使不会成环）也**整批拒绝**。
  同一批次内同一旧号指向两个不同保留号，同样整批拒绝。
- **成环拒绝**：新旧映射合并后若出现环（含 `A→A` 自环），**整批拒绝**。
- **整批原子**：一批 CSV 中任意一行失败，本批任何映射/名单都不会写入。
- **成功合号必须迁移历史名单**：映射写入与名单迁移在同一 SQLite 事务内完成，
  按最终号、分群、业务日去重。
- **后续导入也归并**：分群 CSV 入库时先按当前映射链归到最终号，再按三列去重；
  因某个旧号可能有历史记录，从其导入与从最终号导入结果一致。

示例：

| 操作 | 结果 |
| --- | --- |
| 先导入 `A/VIP/2026-11-01`，再上传 `A→B` | 正式名单变为 `B/VIP/2026-11-01` |
| 已有 `A→B`，再上传 `B→C` | A、B 的历史名单都迁移到 C |
| 已有 `A→B`，再上传 `A→B` | 成功，无数据变化 |
| 已有 `A→B`，再上传 `A→C` | 失败（改向），整批不生效 |
| 已有 `A→B`，再上传 `B→A` | 失败（成环），整批不生效 |
| 同一批含 `X→Y, Y→Z, Z→X` | 失败（成环），整批不生效 |

### 3.2 上传 CSV 格式

两种 CSV 均支持 UTF-8（含 BOM）或 GBK/GB18030，支持带引号、CRLF、空行。

**CRM 合号 CSV**（`旧号, 保留号`）：

```csv
旧号,保留号
A100,B200
B200,C300
D400,A100
```

**分群系统 CSV**（`客户号, 分群, 业务日`）：

```csv
客户号,分群,业务日
A100,高价值,2026-11-01
B200,高价值,2026-11-01
D400,高价值,2026-11-01
C300,高价值,2026-11-01
A100,新客,2026-11-02
```

表头可省略；省略时按上述列顺序读取。有表头时列顺序任意，支持常见中英文列名
（如 `客户编号`、`customer_id`，`客群`、`segment`，`业务日期`、`biz_date`，
`原号`、`old_id`，`保留客户号`、`retained_id` 等）。多余列忽略，缺列/空值整批拒绝。

页面在两处上传区都提供“恢复示例”和“下载示例 CSV”。

### 3.3 页面功能

- 上传 CRM 合号 CSV（文件或文本框内容，文件优先）。
- 上传分群系统 CSV。
- 查看当前直接映射、最终客户号与完整传递链。
- 查看正式名单（分页）以及统计（映射数、名单行数、最终客户数、最长链）。
- 下载**完整**正式名单 CSV，UTF-8 编码，表头为 `客户号,分群,业务日`。
- 上传成功/拒绝原因在页面内即时显示；拒绝时明确提示“整批已拒绝，未写入任何数据”。

### 3.4 并发、一致性与持久化

- 写入：SQLite `BEGIN IMMEDIATE` 将并发上传串行化；一次上传的全部校验、映射写入、
  历史名单迁移、去重只在一个事务内完成，失败/进程中断自动回滚，不会出现
  “映射已更新、名单未迁移”。
- 读取：显式 `BEGIN` 读取事务 + WAL 模式。一次查询或一次下载看到的是归并前或
  归并后的**完整快照**，不会看到迁移到一半的名单；页面概览、映射和名单来自同一快照。
- 两个上传类型可并发提交：无论分群先入库还是合号先入库，最终名单都会归到最终号。
- `PRAGMA synchronous=FULL`；数据库为磁盘文件，容器重启后保留。
- 若旧号指向已存在但不同的保留号，或新旧映射合并后成环，返回 400 和明确错误信息，
  数据库保持原状。

### 3.5 HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/identity/merges/upload` | 上传 CRM 合号 CSV，整批生效 |
| `POST` | `/api/identity/segments/upload` | 上传分群 CSV，整批生效 |
| `GET` | `/api/identity/state?page=1&page_size=20` | 映射 + 统计 + 名单分页（同一快照） |
| `GET` | `/api/identity/merges` | 直接映射、最终号、传递链 |
| `GET` | `/api/identity/segments?page=1&page_size=20` | 正式名单分页 |
| `GET` | `/api/identity/segments/download` | 下载完整正式名单 CSV |
| `GET` | `/api/identity/sample/merges.csv` | 示例合号 CSV |
| `GET` | `/api/identity/sample/segments.csv` | 示例分群 CSV |
| `POST` | `/api/analyze` | 原有 SQL 字段溯源接口 |

上传接口同时接受以下形式（任选其一）：

- `multipart/form-data`：字段名 `file`
- `application/x-www-form-urlencoded`：字段名 `text` 或 `csv`
- `application/json`：`{"text": "..."}` / `{"csv": "..."}`
- 原始请求体

兼容别名：`/api/merge/upload`、`/api/segments/upload`、`/api/segments/download`、`/export.csv`。

```bash
# 上传合号
curl -F "file=@merges.csv" http://localhost:8000/api/identity/merges/upload

# 上传分群
curl -F "file=@segments.csv" http://localhost:8000/api/identity/segments/upload

# 查询状态
curl "http://localhost:8000/api/identity/state?page=1&page_size=20"

# 下载完整正式名单
curl -OJ http://localhost:8000/api/identity/segments/download
```

成功响应（合号示例）：

```json
{
  "ok": true,
  "kind": "merge",
  "submitted_rows": 3,
  "unique_edges": 3,
  "new_edges": 3,
  "unchanged_edges": 0,
  "migrated_segments": 4,
  "removed_duplicates": 3,
  "segment_rows": 2,
  "final_customers": 1
}
```

失败响应（整批拒绝，HTTP 400）：

```json
{
  "ok": false,
  "error": "合并新旧映射后成环：A → B → A，整批已拒绝",
  "error_type": "cycle"
}
```

`error_type` 可能为 `conflict`（改向）、`cycle`（成环）、`row`/`header`/`encoding`
（CSV 行或表头错误）、`empty`、`pagination`、`busy` 等。

---

## 4. SQL 字段溯源（原有功能）

### 4.1 输入格式

**源表结构**（左栏，每行一张表，`#` 注释，空行忽略）：

```
表名(字段1, 字段2*, 字段3, ...)
```

字段名后紧跟 `*` 表示敏感字段，如 `mobile*`、`id_no*`；字段顺序即 `SELECT *`
的展开顺序。标识符支持字母/数字/`_`/`$`，SQL 中还支持 `"..."`、`` `...` ``、`[ ... ]`。

```
customer(cust_id, name, mobile*, id_no*, city)
orders(order_id, cust_id, amount, status)
```

**SQL**（右栏，单条 SELECT，末尾允许一个分号）。

### 4.2 支持的语法白名单（其余一律拒绝并报错）

| 类别 | 支持 |
| --- | --- |
| CTE | 非递归 `WITH`（可多个、可嵌套，内层可见外层）、CTE 列别名清单；拒绝 `RECURSIVE` |
| SELECT | `SELECT [ALL]`、列别名（`AS` 或裸别名）、`*`、`表名.*`/`别名.*` |
| 输出表达式 | 列引用、常量（数字/字符串/NULL）、`+ - * /` 四则运算与括号、`CASE`（简单与搜索式） |
| 谓词（WHERE / ON / CASE WHEN） | 比较、`AND`/`OR`/`NOT`、`[NOT] IN (常量/表达式列表)`、`[NOT] LIKE`、`IS [NOT] NULL` |
| 表连接 | `INNER JOIN`、`LEFT [OUTER] JOIN ... ON` |
| 集合 | `UNION ALL`（按列位置合并依赖） |
| 子查询 | **仅允许出现在 CTE 体内**；FROM 内联子查询、IN/标量子查询等全部拒绝 |

明确拒绝：普通 `UNION`、`INTERSECT`、`EXCEPT`、`DISTINCT`、`GROUP BY`、`HAVING`、
`ORDER BY`、`LIMIT/OFFSET`、窗口、`RIGHT/FULL/CROSS/NATURAL JOIN`、逗号隐式连接、
无 `ON` 的连接、函数调用（含 `CAST`/`COALESCE`）、`BETWEEN`、`EXISTS`、
字符串连接 `||`、取模 `%`、多语句、任何非 SELECT 语句。

### 4.3 两类依赖

- **值依赖（value）**：输出表达式实际引用的列。沿输出表达式传播；`CASE` 的
  `WHEN` 条件、简单 CASE 的操作数、`THEN/ELSE` 全部计入。
- **条件依赖（condition）**：沿途 `WHERE` 与 `JOIN ON` 中出现的列，作用于所在
  查询块的全部输出列（即使该输出表达式并未引用它们），并随 CTE/UNION 继续传递。

一个源字段可同时属于两类。前端用红色（值敏感）、橙色（条件敏感）、紫色（兼有）区分。

传播规则要点：

- 星号按源表字段顺序展开；`表名.*` 仅展开该来源。
- `UNION ALL` 按列位置对齐，合并各分支同位置列的全部依赖；列数不一致报错。
- 未知表/列、裸列名在多表中产生歧义、限定名不存在等情况一律报错，不做猜测。
- 常量输出列没有值依赖；但只要所在查询块存在 WHERE/ON，仍会带条件依赖。

### 4.4 溯源接口

`POST /api/analyze`：

```json
{ "schema": "customer(cust_id, mobile*)", "sql": "SELECT cust_id FROM customer" }
```

成功 `200`：`{"ok": true, "result": {"outputs": [...], "ctes": [...], "base_tables": [...]}}`；
语法/溯源错误 `400`：`{"ok": false, "error": "第 x 行第 y 列：..."}`。

---

## 5. 目录结构

```
.
├── docker-compose.yml               # 服务编排、端口 8000、identity-data 命名卷
├── run-local.sh                     # 本地启动脚本
├── app
│   ├── Dockerfile                   # python:3.11-slim + gunicorn
│   ├── requirements.txt
│   ├── server.py                    # Flask 路由：溯源 + 身份归并
│   ├── identity
│   │   ├── store.py                 # SQLite 事务、链路解析、名单迁移/去重
│   │   ├── csv_io.py                # CSV 解析/导出
│   │   ├── models.py                # 带行号的 CSV 行模型
│   │   ├── errors.py                # 业务错误
│   │   └── samples.py               # 页面示例 CSV
│   ├── lineage                     # 原有溯源引擎（仅标准库）
│   ├── templates/index.html         # 溯源页（含身份归并入口）
│   ├── templates/identity.html      # 身份归并页
│   └── static/{style.css,app.js,identity.js}
└── tests
    ├── test_lineage.py              # 溯源 62 个用例
    └── test_identity.py             # 归并/原子性/并发/CSV 等用例
```

## 6. 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：CSV 表头/编码/空行、链路解析、历史名单迁移与去重、同向重传幂等、
改向拒绝、自环/跨链成环拒绝、整批原子性、迁移中断回滚、读取快照一致性、
并发合号与分群导入、分页、导出、重启后持久化，以及原有 62 个溯源用例。
