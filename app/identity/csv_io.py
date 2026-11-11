"""分群/合号 CSV 的解析与正式名单导出（仅 Python 标准库）。

输入约定
--------
合号 CSV（CRM）：
    旧号, 保留号
分群 CSV（分群系统）：
    客户号, 分群, 业务日

两种 CSV 都允许：
  * UTF-8（含 BOM）或 GBK/GB18030 编码；
  * 带表头或不带表头（中文/常见英文列名均可，列顺序任意）；
  * 不带表头时按上述列顺序读取，多余列忽略；
  * 带引号、CRLF、空行。

任何一行缺列/空值都会抛 :class:`IdentityError`，由上层整批拒绝。
"""

import csv
import io
import re

from .errors import IdentityError
from .models import MergeRow, SegmentRow

# 表头归一化：去空白/下划线/连字符并转小写，中文保持不变。
def _norm_header(value):
    return re.sub(r"[\s_\-]+", "", (value or "").lstrip("﻿")).lower()


def _alias_set(values):
    return {_norm_header(v) for v in values}


_CUSTOMER_ALIASES = _alias_set([
    "客户号", "客户编号", "客户代码", "客户id", "客户",
    "cust_id", "custid", "customer_id", "customerid",
    "customer_no", "customerno", "cust_no", "custno", "id", "user_id", "会员号",
])

_SEGMENT_ALIASES = _alias_set([
    "分群", "客群", "人群", "分群名称", "客群名称",
    "segment", "segment_name", "segmentname", "group", "group_name", "groupname",
])

_BIZ_DATE_ALIASES = _alias_set([
    "业务日", "业务日期", "日期",
    "biz_date", "bizdate", "business_date", "businessdate", "date", "dt", "day",
])

_OLD_ID_ALIASES = _alias_set([
    "旧号", "旧客户号", "原号", "原客户号", "老号",
    "old_id", "oldid", "old_cust_id", "oldcustid",
    "old_customer_id", "oldcustomerid", "from_id", "fromid", "source_id", "sourceid",
])

_RETAINED_ID_ALIASES = _alias_set([
    "保留号", "保留客户号", "新号", "新客户号", "最终号", "最终客户号", "合并后客户号",
    "retained_id", "retainedid", "retained_cust_id", "retainedcustid",
    "new_id", "newid", "new_cust_id", "newcustid",
    "to_id", "toid", "target_id", "targetid",
])

_MERGE_ROLES = {
    "old": (_OLD_ID_ALIASES, "旧号"),
    "retained": (_RETAINED_ID_ALIASES, "保留号"),
}

_SEGMENT_ROLES = {
    "cust": (_CUSTOMER_ALIASES, "客户号"),
    "segment": (_SEGMENT_ALIASES, "分群"),
    "biz_date": (_BIZ_DATE_ALIASES, "业务日"),
}

_EXPORT_HEADER = ["客户号", "分群", "业务日"]


def decode_csv_bytes(raw):
    """把上传的字节流解码为文本；字符串原样返回。"""
    if isinstance(raw, str):
        return raw
    if raw is None:
        return ""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise IdentityError("无法识别 CSV 编码，请使用 UTF-8 或 GBK/GB18030 编码", "encoding")


def _read_rows(text):
    """返回 [(行号, [单元格...])]，跳过空行。"""
    if not isinstance(text, str):
        text = decode_csv_bytes(text)
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    rows = []
    try:
        for row in reader:
            cells = [cell.strip() for cell in row]
            if not any(cells):
                continue
            rows.append((reader.line_num, cells))
    except csv.Error as exc:  # pragma: no cover - 防御性兜底
        raise IdentityError(f"CSV 解析失败：{exc}", "csv") from exc
    return rows


def _is_header(cells, aliases_by_role):
    all_aliases = set()
    for aliases, _label in aliases_by_role.values():
        all_aliases |= aliases
    return any(_norm_header(cell) in all_aliases for cell in cells)


def _header_indices(cells, aliases_by_role, csv_name):
    indices = {}
    for role, (aliases, label) in aliases_by_role.items():
        for index, cell in enumerate(cells):
            if _norm_header(cell) in aliases:
                indices[role] = index
                break
    missing = [label for role, (_aliases, label) in aliases_by_role.items()
               if role not in indices]
    if missing:
        raise IdentityError(
            f"{csv_name}表头缺少列：{'、'.join(missing)}。"
            f"可用列名示例：{'、'.join(label for _a, label in aliases_by_role.values())}。",
            "header",
        )
    return indices


def _body_rows(rows, aliases_by_role, csv_name, positional_indices):
    """识别表头并返回（索引映射，数据行列表）。"""
    if not rows:
        raise IdentityError(f"{csv_name}为空：没有表头也没有数据行，整批已拒绝", "empty")

    first_line, first_cells = rows[0]
    if _is_header(first_cells, aliases_by_role):
        indices = _header_indices(first_cells, aliases_by_role, csv_name)
        data = rows[1:]
    else:
        indices = positional_indices
        data = rows

    if not data:
        raise IdentityError(f"{csv_name}只有表头，没有数据行，整批已拒绝", "empty")

    required = max(indices.values()) + 1
    for role, label in ((r, info[1]) for r, info in aliases_by_role.items()):
        if role not in indices:  # pragma: no cover - 已由表头检查/位置映射保证
            raise IdentityError(f"{csv_name}缺少列 {label}", "header")
    for line, cells in data:
        if len(cells) < required:
            raise IdentityError(
                f"{csv_name}第 {line} 行列数不足：至少需要 {required} 列，"
                f"实际 {len(cells)} 列，整批已拒绝",
                "row",
            )
    return indices, data


def parse_merge_csv(source):
    """解析 CRM 合号 CSV，返回 :class:`MergeRow` 列表。"""
    rows = _read_rows(source)
    indices, data = _body_rows(
        rows, _MERGE_ROLES, "合号 CSV",
        {"old": 0, "retained": 1},
    )
    old_index = indices["old"]
    retained_index = indices["retained"]

    parsed = []
    for line, cells in data:
        old_id = cells[old_index].strip()
        retained_id = cells[retained_index].strip()
        if not old_id or not retained_id:
            raise IdentityError(
                f"合号 CSV第 {line} 行：旧号和保留号均不能为空，整批已拒绝",
                "row",
            )
        parsed.append(MergeRow(old_id=old_id, new_id=retained_id, line=line))
    return parsed


def parse_segments_csv(source):
    """解析分群系统 CSV，返回 :class:`SegmentRow` 列表。"""
    rows = _read_rows(source)
    indices, data = _body_rows(
        rows, _SEGMENT_ROLES, "分群 CSV",
        {"cust": 0, "segment": 1, "biz_date": 2},
    )
    cust_index = indices["cust"]
    segment_index = indices["segment"]
    date_index = indices["biz_date"]

    parsed = []
    for line, cells in data:
        cust_id = cells[cust_index].strip()
        segment = cells[segment_index].strip()
        biz_date = cells[date_index].strip()
        if not cust_id or not segment or not biz_date:
            raise IdentityError(
                f"分群 CSV第 {line} 行：客户号、分群、业务日均不能为空，整批已拒绝",
                "row",
            )
        parsed.append(SegmentRow(cust_id=cust_id, segment=segment, biz_date=biz_date, line=line))
    return parsed


def format_segments_csv(rows, header=True):
    """把正式名单行（二元组/字典/对象）导出为 CSV 文本。"""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    if header:
        writer.writerow(_EXPORT_HEADER)
    for row in rows:
        if isinstance(row, dict):
            values = (row.get("cust_id"), row.get("segment"), row.get("biz_date"))
        elif hasattr(row, "cust_id"):
            values = (row.cust_id, row.segment, row.biz_date)
        else:
            values = tuple(row)
        writer.writerow(values)
    return output.getvalue()
