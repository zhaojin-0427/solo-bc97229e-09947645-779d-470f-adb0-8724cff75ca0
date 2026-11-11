"""SQLite 持久化：直接合号映射 + 正式分群名单。

关键保证
--------
* 写入使用 ``BEGIN IMMEDIATE``，让并发上传串行化；一次上传只用一个事务，
  事务内完成“校验映射 / 写入映射 / 迁移历史名单 / 去重”，失败或进程中断
  时整体回滚，不会留下“映射已更新、名单未迁移”的中间态。
* 读取使用 ``BEGIN`` 显式事务，配合 WAL 模式，查询/下载看到的是一个完整
  快照（归并前或归并后），不会看到迁移到一半的名单。
* 数据库为磁盘文件（默认 app/data/identity.db），容器重启后数据保留。
"""

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .errors import IdentityError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS direct_merges (
    old_id     TEXT PRIMARY KEY,
    new_id     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS formal_segments (
    cust_id  TEXT NOT NULL,
    segment  TEXT NOT NULL,
    biz_date TEXT NOT NULL,
    PRIMARY KEY (cust_id, segment, biz_date)
);

CREATE INDEX IF NOT EXISTS idx_formal_segments_cust
    ON formal_segments (cust_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_busy(exc):
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _db_busy_error(exc):
    return IdentityError("数据库繁忙（并发写入未能在超时内完成），本次整批已拒绝，请重试", "busy")


class IdentityStore:
    """直接合号映射与正式名单的唯一写入/读取入口。"""

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------ 初始化
    def _init_db(self):
        """创建表并启用 WAL；多进程同时启动时对 locked 做重试。"""
        last_error = None
        with self._init_lock:
            for attempt in range(20):
                conn = None
                try:
                    conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
                    conn.execute("PRAGMA busy_timeout = 30000")
                    conn.execute("PRAGMA journal_mode = WAL").fetchone()
                    conn.execute("PRAGMA synchronous = FULL")
                    conn.executescript(_SCHEMA)
                    return
                except sqlite3.OperationalError as exc:
                    last_error = exc
                    if _is_busy(exc):
                        time.sleep(0.1 + 0.05 * attempt)
                        continue
                    raise
                finally:
                    if conn is not None:
                        conn.close()
        raise IdentityError(f"初始化数据库失败：{last_error}", "db")

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        # WAL + FULL：进程中断后已提交事务仍持久，未提交事务自动回滚。
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def _write_txn(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def _read_txn(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ 图计算
    @staticmethod
    def _graph_from(conn):
        return {
            row["old_id"]: row["new_id"]
            for row in conn.execute("SELECT old_id, new_id FROM direct_merges")
        }

    @staticmethod
    def _resolve(cust_id, graph):
        """沿直接映射链走到最终客户号；遇到环则拒绝。"""
        seen = set()
        current = cust_id
        while current in graph:
            if current in seen:
                raise IdentityError(f"合号映射成环，无法解析客户号 {cust_id}", "cycle")
            seen.add(current)
            current = graph[current]
        return current

    @staticmethod
    def _find_cycle(graph):
        """在“每个节点最多一个直接去向”的图中找环，返回环路径或 None。"""
        done = set()
        for start in graph:
            if start in done:
                continue
            positions = {}
            path = []
            node = start
            while node in graph and node not in positions and node not in done:
                positions[node] = len(path)
                path.append(node)
                node = graph[node]
            if node in positions:
                return path[positions[node]:] + [node]
            done.update(path)
        return None

    @staticmethod
    def _describe_merges(graph):
        described = []
        for old_id in sorted(graph):
            chain = [old_id]
            seen = {old_id}
            current = old_id
            while current in graph:
                nxt = graph[current]
                if nxt in seen:  # 正常数据不会走到这里；防御性保护
                    break
                seen.add(nxt)
                chain.append(nxt)
                current = nxt
            described.append({
                "old_id": old_id,
                "new_id": graph[old_id],
                "final_id": chain[-1],
                "chain": chain,
            })
        return described

    # ------------------------------------------------------------------ 写：合号
    def upload_merges(self, rows):
        """整批上传 CRM 合号映射；成功时在同一事务内迁移历史名单。

        - 同一旧号同向重传：不改变任何数据；
        - 同一旧号改向（含同批次内改向）：整批拒绝；
        - 合并新旧映射后成环：整批拒绝。
        """
        if not rows:
            raise IdentityError("合号 CSV 没有数据行，整批已拒绝", "empty")

        submitted = len(rows)
        proposed = {}
        duplicate_rows = 0
        for row in rows:
            old_id = (row.old_id or "").strip()
            new_id = (row.new_id or "").strip()
            line = getattr(row, "line", 0)
            if not old_id or not new_id:
                raise IdentityError(
                    f"合号 CSV第 {line} 行：旧号和保留号均不能为空，整批已拒绝", "row")
            if old_id in proposed:
                if proposed[old_id] != new_id:
                    raise IdentityError(
                        f"合号 CSV第 {line} 行：同一批次内旧号 {old_id} 既指向 "
                        f"{proposed[old_id]} 又指向 {new_id}，不允许改向，整批已拒绝",
                        "conflict")
                duplicate_rows += 1
                continue
            proposed[old_id] = new_id

        try:
            with self._write_txn() as conn:
                existing = self._graph_from(conn)

                # 改向一律拒绝：直接去向一旦落库不可更改。
                for old_id, new_id in proposed.items():
                    if old_id in existing and existing[old_id] != new_id:
                        raise IdentityError(
                            f"旧号 {old_id} 已直接指向 {existing[old_id]}，"
                            f"本次改为 {new_id}：不允许改向，整批已拒绝",
                            "conflict")

                # 新旧映射合并后检查环（含自环）。
                graph = dict(existing)
                graph.update(proposed)
                cycle = self._find_cycle(graph)
                if cycle:
                    raise IdentityError(
                        "合并新旧映射后成环：" + " → ".join(cycle) + "，整批已拒绝",
                        "cycle")

                changed = [old for old, new in proposed.items()
                           if existing.get(old) != new]
                now = _utcnow()
                for old_id in changed:
                    conn.execute(
                        "INSERT INTO direct_merges(old_id, new_id, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (old_id, proposed[old_id], now, now),
                    )

                migrated = 0
                removed_duplicates = 0
                if changed:
                    migrated, removed_duplicates = self._migrate_segments(conn, graph)

                segment_rows = conn.execute(
                    "SELECT COUNT(*) FROM formal_segments").fetchone()[0]
                final_customers = conn.execute(
                    "SELECT COUNT(DISTINCT cust_id) FROM formal_segments").fetchone()[0]
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise _db_busy_error(exc) from exc
            raise

        return {
            "kind": "merge",
            "submitted_rows": submitted,
            "unique_edges": len(proposed),
            "new_edges": len(changed),
            "unchanged_edges": len(proposed) - len(changed),
            "duplicate_rows": duplicate_rows,
            "migrated_segments": migrated,
            "removed_duplicates": removed_duplicates,
            "segment_rows": segment_rows,
            "final_customers": final_customers,
        }

    def _migrate_segments(self, conn, graph):
        """把历史名单中的每个客户号沿链改到最终号，再按主键去重。

        必须在调用方的写事务内执行；返回（改号行数, 去重删除行数）。
        """
        rows = conn.execute(
            "SELECT cust_id, segment, biz_date FROM formal_segments").fetchall()
        rebuilt = {}
        migrated = 0
        for row in rows:
            cust_id = row["cust_id"]
            final_id = self._resolve(cust_id, graph)
            if final_id != cust_id:
                migrated += 1
            rebuilt[(final_id, row["segment"], row["biz_date"])] = True

        removed_duplicates = len(rows) - len(rebuilt)
        conn.execute("DELETE FROM formal_segments")
        if rebuilt:
            conn.executemany(
                "INSERT INTO formal_segments(cust_id, segment, biz_date) VALUES (?, ?, ?)",
                [(cust_id, segment, biz_date)
                 for (cust_id, segment, biz_date) in rebuilt],
            )
        return migrated, removed_duplicates

    # ------------------------------------------------------------------ 写：分群
    def upload_segments(self, rows):
        """整批导入分群名单：先按当前合号链归到最终号，再按三列主键去重。"""
        if not rows:
            raise IdentityError("分群 CSV 没有数据行，整批已拒绝", "empty")

        cleaned = []
        for row in rows:
            cust_id = (row.cust_id or "").strip()
            segment = (row.segment or "").strip()
            biz_date = (row.biz_date or "").strip()
            line = getattr(row, "line", 0)
            if not cust_id or not segment or not biz_date:
                raise IdentityError(
                    f"分群 CSV第 {line} 行：客户号、分群、业务日均不能为空，整批已拒绝",
                    "row")
            cleaned.append((cust_id, segment, biz_date))

        inserted = 0
        resolved_to_final = 0
        try:
            with self._write_txn() as conn:
                graph = self._graph_from(conn)
                for cust_id, segment, biz_date in cleaned:
                    final_id = self._resolve(cust_id, graph)
                    if final_id != cust_id:
                        resolved_to_final += 1
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO formal_segments(cust_id, segment, biz_date) "
                        "VALUES (?, ?, ?)",
                        (final_id, segment, biz_date),
                    )
                    if cursor.rowcount > 0:
                        inserted += 1

                segment_rows = conn.execute(
                    "SELECT COUNT(*) FROM formal_segments").fetchone()[0]
                final_customers = conn.execute(
                    "SELECT COUNT(DISTINCT cust_id) FROM formal_segments").fetchone()[0]
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise _db_busy_error(exc) from exc
            raise

        return {
            "kind": "segment",
            "submitted_rows": len(cleaned),
            "inserted_rows": inserted,
            "ignored_rows": len(cleaned) - inserted,
            "resolved_to_final": resolved_to_final,
            "segment_rows": segment_rows,
            "final_customers": final_customers,
        }

    # ------------------------------------------------------------------ 读
    @staticmethod
    def _normalize_page(page, page_size):
        try:
            page = int(page)
            page_size = int(page_size)
        except (TypeError, ValueError) as exc:
            raise IdentityError("分页参数 page / page_size 必须为整数", "pagination") from exc
        if page < 1:
            raise IdentityError("分页参数 page 必须从 1 开始", "pagination")
        if page_size < 1:
            raise IdentityError("分页参数 page_size 必须大于 0", "pagination")
        return page, min(page_size, 500)

    @staticmethod
    def _segment_page(conn, page, page_size):
        total = conn.execute("SELECT COUNT(*) FROM formal_segments").fetchone()[0]
        rows = conn.execute(
            "SELECT cust_id, segment, biz_date FROM formal_segments "
            "ORDER BY cust_id, segment, biz_date LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        ).fetchall()
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "rows": [
                {"cust_id": r["cust_id"], "segment": r["segment"], "biz_date": r["biz_date"]}
                for r in rows
            ],
        }

    def get_state(self, page=1, page_size=20):
        """返回映射、统计和当前页名单；三者来自同一个读取快照。"""
        page, page_size = self._normalize_page(page, page_size)
        try:
            with self._read_txn() as conn:
                graph = self._graph_from(conn)
                merges = self._describe_merges(graph)
                segment_page = self._segment_page(conn, page, page_size)
                stats = {
                    "merge_edges": len(graph),
                    "segment_rows": segment_page["total"],
                    "final_customers": conn.execute(
                        "SELECT COUNT(DISTINCT cust_id) FROM formal_segments").fetchone()[0],
                    "max_chain_length": max((len(m["chain"]) for m in merges), default=0),
                }
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise _db_busy_error(exc) from exc
            raise
        return {"merges": merges, "stats": stats, "segments": segment_page}

    def list_segments(self, page=1, page_size=20):
        """分页查询正式名单（单次请求一个完整快照）。"""
        page, page_size = self._normalize_page(page, page_size)
        try:
            with self._read_txn() as conn:
                return self._segment_page(conn, page, page_size)
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise _db_busy_error(exc) from exc
            raise

    def export_segments(self):
        """在单个读取事务中取出完整正式名单，供一次性 CSV 下载。"""
        try:
            with self._read_txn() as conn:
                rows = conn.execute(
                    "SELECT cust_id, segment, biz_date FROM formal_segments "
                    "ORDER BY cust_id, segment, biz_date"
                ).fetchall()
                return [(r["cust_id"], r["segment"], r["biz_date"]) for r in rows]
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise _db_busy_error(exc) from exc
            raise

    def list_merges(self):
        """返回当前直接映射及最终号/传递链。"""
        try:
            with self._read_txn() as conn:
                graph = self._graph_from(conn)
                return self._describe_merges(graph)
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise _db_busy_error(exc) from exc
            raise
