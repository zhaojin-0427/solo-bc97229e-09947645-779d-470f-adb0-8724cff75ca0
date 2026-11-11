"""身份归并入仓核心逻辑测试（仅标准库）。

运行：python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from identity import (  # noqa: E402
    IdentityError,
    IdentityStore,
    MergeRow,
    SegmentRow,
    format_segments_csv,
    parse_merge_csv,
    parse_segments_csv,
)


class IdentityStoreTest(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "identity.db")
        self.store = IdentityStore(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    # -------------------------------------------------------------- 辅助方法
    def merge(self, csv_text):
        return self.store.upload_merges(parse_merge_csv(csv_text))

    def segments(self, csv_text):
        return self.store.upload_segments(parse_segments_csv(csv_text))

    def export_rows(self):
        return self.store.export_segments()

    def golden_ids(self):
        return [row[0] for row in self.export_rows()]

    def merge_ids(self):
        return {m["old_id"]: m["new_id"] for m in self.store.list_merges()}

    # -------------------------------------------------------------- 基础导入
    def test_segment_import_without_merge(self):
        result = self.segments("""客户号,分群,业务日
C001,高价值,2026-11-01
C002,新客,2026-11-02
""")
        self.assertEqual(result["inserted_rows"], 2)
        self.assertEqual(self.export_rows(), [
            ("C001", "高价值", "2026-11-01"),
            ("C002", "新客", "2026-11-02"),
        ])

    def test_segment_import_dedupes_on_final_id_segment_and_date(self):
        result = self.segments("""客户号,分群,业务日
C001,高价值,2026-11-01
C001,高价值,2026-11-01
C001,新客,2026-11-02
""")
        self.assertEqual(result["submitted_rows"], 3)
        self.assertEqual(result["inserted_rows"], 2)
        self.assertEqual(result["ignored_rows"], 1)
        self.assertEqual(self.golden_ids(), ["C001", "C001"])

    # -------------------------------------------------------------- 合号迁移
    def test_merge_migrates_history_and_dedupes(self):
        self.segments("""客户号,分群,业务日
A100,VIP,2026-11-01
B200,VIP,2026-11-01
C300,VIP,2026-11-01
A100,新客,2026-11-02
""")
        result = self.merge("""旧号,保留号
A100,B200
""")
        self.assertEqual(result["new_edges"], 1)
        self.assertEqual(result["migrated_segments"], 2)
        self.assertEqual(result["removed_duplicates"], 1)
        self.assertEqual(self.export_rows(), [
            ("B200", "VIP", "2026-11-01"),
            ("B200", "新客", "2026-11-02"),
            ("C300", "VIP", "2026-11-01"),
        ])

    def test_chain_resolves_to_final_customer(self):
        self.segments("""客户号,分群,业务日
A100,VIP,2026-11-01
""")
        self.merge("""旧号,保留号
A100,B200
B200,C300
""")
        self.assertEqual(self.golden_ids(), ["C300"])
        merges = self.store.list_merges()
        self.assertEqual(merges[0]["final_id"], "C300")
        self.assertEqual(merges[0]["chain"], ["A100", "B200", "C300"])

    def test_chain_extension_migrates_again(self):
        self.segments("""客户号,分群,业务日
A100,VIP,2026-11-01
""")
        self.merge("旧号,保留号\nA100,B200\n")
        self.assertEqual(self.golden_ids(), ["B200"])
        result = self.merge("旧号,保留号\nB200,C300\n")
        self.assertEqual(result["new_edges"], 1)
        self.assertEqual(self.golden_ids(), ["C300"])

    def test_import_after_merge_uses_final_id(self):
        self.merge("""旧号,保留号
A100,B200
B200,C300
""")
        self.segments("""客户号,分群,业务日
A100,VIP,2026-11-01
B200,VIP,2026-11-01
C300,VIP,2026-11-01
A100,新客,2026-11-02
""")
        self.assertEqual(self.export_rows(), [
            ("C300", "VIP", "2026-11-01"),
            ("C300", "新客", "2026-11-02"),
        ])

    def test_same_direction_reupload_is_idempotent(self):
        self.segments("""客户号,分群,业务日
A100,VIP,2026-11-01
""")
        first = self.merge("旧号,保留号\nA100,B200\n")
        self.assertEqual(first["new_edges"], 1)
        before = self.export_rows()

        again = self.merge("旧号,保留号\nA100,B200\n")
        self.assertEqual(again["new_edges"], 0)
        self.assertEqual(again["unchanged_edges"], 1)
        self.assertEqual(again["migrated_segments"], 0)
        self.assertEqual(self.merge_ids(), {"A100": "B200"})
        self.assertEqual(self.export_rows(), before)

    def test_duplicate_same_direction_rows_in_one_batch(self):
        result = self.merge("旧号,保留号\nA100,B200\nA100,B200\n")
        self.assertEqual(result["submitted_rows"], 2)
        self.assertEqual(result["unique_edges"], 1)
        self.assertEqual(result["new_edges"], 1)
        self.assertEqual(result["duplicate_rows"], 1)

    # -------------------------------------------------------------- 拒绝：改向
    def test_direction_change_rejected_atomically(self):
        self.merge("""旧号,保留号
A100,B200
C300,D400
""")
        with self.assertRaises(IdentityError) as ctx:
            self.merge("""旧号,保留号
A100,E500
F600,G700
""")
        self.assertEqual(ctx.exception.code, "conflict")
        # 合法的新映射 F600→G700 也不能落库（整批拒绝）
        self.assertEqual(self.merge_ids(), {"A100": "B200", "C300": "D400"})
        self.assertEqual(self.export_rows(), [])

    def test_direction_change_in_same_batch_rejected(self):
        with self.assertRaises(IdentityError) as ctx:
            self.merge("旧号,保留号\nA100,B200\nA100,C300\n")
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertEqual(self.merge_ids(), {})

    def test_direction_change_keeps_existing_history_unchanged(self):
        self.segments("客户号,分群,业务日\nA100,VIP,2026-11-01\n")
        self.merge("旧号,保留号\nA100,B200\n")
        with self.assertRaises(IdentityError):
            self.merge("旧号,保留号\nA100,C300\n")
        self.assertEqual(self.export_rows(), [("B200", "VIP", "2026-11-01")])

    # -------------------------------------------------------------- 拒绝：成环
    def test_cycle_with_existing_mapping_rejected(self):
        self.merge("旧号,保留号\nA100,B200\n")
        with self.assertRaises(IdentityError) as ctx:
            self.merge("旧号,保留号\nB200,A100\n")
        self.assertEqual(ctx.exception.code, "cycle")
        self.assertEqual(self.merge_ids(), {"A100": "B200"})

    def test_self_merge_rejected(self):
        with self.assertRaises(IdentityError) as ctx:
            self.merge("旧号,保留号\nA100,A100\n")
        self.assertEqual(ctx.exception.code, "cycle")
        self.assertEqual(self.merge_ids(), {})

    def test_cycle_across_existing_chain_rejected(self):
        self.merge("""旧号,保留号
A100,B200
B200,C300
""")
        with self.assertRaises(IdentityError) as ctx:
            self.merge("旧号,保留号\nC300,A100\n")
        self.assertEqual(ctx.exception.code, "cycle")
        self.assertEqual(self.merge_ids(), {"A100": "B200", "B200": "C300"})

    def test_cycle_in_one_batch_rejected_atomically(self):
        with self.assertRaises(IdentityError) as ctx:
            self.merge("""旧号,保留号
X1,Y1
Y1,Z1
Z1,X1
""")
        self.assertEqual(ctx.exception.code, "cycle")
        self.assertEqual(self.merge_ids(), {})

    def test_conflict_plus_cycle_rejects_whole_batch(self):
        self.segments("客户号,分群,业务日\nA100,VIP,2026-11-01\n")
        self.merge("旧号,保留号\nA100,B200\n")
        with self.assertRaises(IdentityError):
            self.merge("""旧号,保留号
B200,C300
C300,B200
A100,D400
""")
        self.assertEqual(self.merge_ids(), {"A100": "B200"})
        self.assertEqual(self.export_rows(), [("B200", "VIP", "2026-11-01")])

    # -------------------------------------------------------------- 事务原子性
    def test_failure_during_migration_rolls_back_mapping(self):
        self.segments("客户号,分群,业务日\nA100,VIP,2026-11-01\n")
        original = self.store._migrate_segments

        def boom(conn, graph):
            raise RuntimeError("模拟迁移过程中断")

        self.store._migrate_segments = boom
        try:
            with self.assertRaises(RuntimeError):
                self.merge("旧号,保留号\nA100,B200\n")
        finally:
            self.store._migrate_segments = original

        # 映射与名单都必须保持归并前状态
        self.assertEqual(self.merge_ids(), {})
        self.assertEqual(self.export_rows(), [("A100", "VIP", "2026-11-01")])

    def test_read_snapshot_is_complete_before_and_after_merge(self):
        self.segments("客户号,分群,业务日\nA100,VIP,2026-11-01\n")
        started = threading.Event()
        release = threading.Event()
        original = self.store._migrate_segments

        def blocked(conn, graph):
            started.set()
            if not release.wait(10):
                raise RuntimeError("测试等待超时")
            return original(conn, graph)

        self.store._migrate_segments = blocked
        errors = []

        def writer():
            try:
                self.merge("旧号,保留号\nA100,B200\n")
            except BaseException as exc:  # pragma: no cover - 测试失败时记录
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(started.wait(10))
            # 此时写入事务未提交：读取必须看到完整的“归并前”状态
            snapshot = self.store.get_state(page=1, page_size=50)
            self.assertEqual(snapshot["merges"], [])
            self.assertEqual([r["cust_id"] for r in snapshot["segments"]["rows"]], ["A100"])
        finally:
            release.set()
            thread.join(10)
            self.store._migrate_segments = original

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        final = self.store.get_state(page=1, page_size=50)
        self.assertEqual(final["merges"][0]["final_id"], "B200")
        self.assertEqual([r["cust_id"] for r in final["segments"]["rows"]], ["B200"])

    def test_persistence_across_store_reopen(self):
        self.merge("旧号,保留号\nA100,B200\n")
        self.segments("客户号,分群,业务日\nA100,VIP,2026-11-01\n")
        reopened = IdentityStore(self.db_path)
        state = reopened.get_state(page=1, page_size=50)
        self.assertEqual(state["merges"][0]["new_id"], "B200")
        self.assertEqual([r["cust_id"] for r in state["segments"]["rows"]], ["B200"])

    # -------------------------------------------------------------- 并发
    def test_concurrent_merge_and_segment_uploads_stay_consistent(self):
        pairs = 6
        barrier = threading.Barrier(pairs * 2)
        errors = []

        def merge_worker(index):
            try:
                barrier.wait(10)
                self.store.upload_merges([MergeRow(f"A{index}", f"B{index}", 0)])
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        def segment_worker(index):
            try:
                barrier.wait(10)
                self.store.upload_segments([
                    SegmentRow(f"A{index}", f"S{index}", "2026-11-01", 0)
                ])
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = []
        for index in range(pairs):
            threads.append(threading.Thread(target=merge_worker, args=(index,)))
            threads.append(threading.Thread(target=segment_worker, args=(index,)))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(self.store.list_merges()), pairs)
        rows = self.export_rows()
        self.assertEqual(len(rows), pairs)
        # 无论分群先入库还是合号先入库，最终名单都只能出现最终客户号
        self.assertEqual({row[0] for row in rows}, {f"B{i}" for i in range(pairs)})

    # -------------------------------------------------------------- 查询/导出
    def test_pagination_and_export_csv(self):
        rows = [SegmentRow(f"C{i:03d}", "S", "2026-11-01", 0) for i in range(25)]
        result = self.store.upload_segments(rows)
        self.assertEqual(result["inserted_rows"], 25)

        page = self.store.list_segments(page=2, page_size=10)
        self.assertEqual(page["total"], 25)
        self.assertEqual(page["page"], 2)
        self.assertEqual(page["page_size"], 10)
        self.assertEqual(len(page["rows"]), 10)
        self.assertEqual(page["rows"][0]["cust_id"], "C010")

        exported = self.store.export_segments()
        self.assertEqual(len(exported), 25)
        text = format_segments_csv(exported)
        self.assertTrue(text.startswith("客户号,分群,业务日\n"))
        reparsed = parse_segments_csv(text)
        self.assertEqual(len(reparsed), 25)

    def test_invalid_pagination_rejected(self):
        with self.assertRaises(IdentityError):
            self.store.list_segments(page=0, page_size=10)
        with self.assertRaises(IdentityError):
            self.store.list_segments(page=1, page_size=0)


class CsvParsingTest(unittest.TestCase):

    def test_chinese_header_with_reordered_columns(self):
        rows = parse_segments_csv("""业务日,客户号,分群
2026-11-01,C001,VIP
""")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].cust_id, rows[0].segment, rows[0].biz_date),
                         ("C001", "VIP", "2026-11-01"))

        merges = parse_merge_csv("""保留号,旧号
B200,A100
""")
        self.assertEqual((merges[0].old_id, merges[0].new_id), ("A100", "B200"))

    def test_english_header_and_no_header(self):
        rows = parse_segments_csv("""customer_id,segment,biz_date
C001,VIP,2026-11-01
""")
        self.assertEqual(rows[0].cust_id, "C001")

        rows = parse_segments_csv("C001,VIP,2026-11-01\nC002,新客,2026-11-02\n")
        self.assertEqual(len(rows), 2)

        merges = parse_merge_csv("old_id,new_id\nA100,B200\n")
        self.assertEqual(merges[0].new_id, "B200")

        merges = parse_merge_csv("A100,B200\n")
        self.assertEqual(merges[0].old_id, "A100")

    def test_bom_gbk_blank_lines_and_quotes(self):
        text = "﻿客户号,分群,业务日\n\nC001,\"高,价值\",2026-11-01\r\n"
        rows = parse_segments_csv(text)
        self.assertEqual(rows[0].segment, "高,价值")

        gbk_bytes = "客户号,分群,业务日\nC001,VIP,2026-11-01\n".encode("gbk")
        rows = parse_segments_csv(gbk_bytes)
        self.assertEqual(rows[0].cust_id, "C001")

    def test_empty_and_header_only_rejected(self):
        for text in ("", "   \n", "客户号,分群,业务日\n", "旧号,保留号\n"):
            with self.assertRaises(IdentityError):
                parse_segments_csv(text) if "分群" in text else parse_merge_csv(text)

    def test_missing_header_column_rejected(self):
        with self.assertRaises(IdentityError) as ctx:
            parse_segments_csv("客户号,分群\nC001,VIP\n")
        self.assertEqual(ctx.exception.code, "header")

        with self.assertRaises(IdentityError) as ctx:
            parse_merge_csv("旧号\nA100\n")
        self.assertEqual(ctx.exception.code, "header")

    def test_row_with_empty_or_missing_values_rejected(self):
        with self.assertRaises(IdentityError):
            parse_segments_csv("客户号,分群,业务日\nC001,VIP\n")
        with self.assertRaises(IdentityError):
            parse_segments_csv("客户号,分群,业务日\n,VIP,2026-11-01\n")
        with self.assertRaises(IdentityError):
            parse_merge_csv("旧号,保留号\nA100,\n")
        with self.assertRaises(IdentityError):
            parse_merge_csv("旧号,保留号\n,B200\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
