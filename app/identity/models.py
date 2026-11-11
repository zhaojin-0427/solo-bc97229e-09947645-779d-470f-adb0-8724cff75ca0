"""CSV 解析后的行模型（带原始行号，便于整批拒绝时定位）。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MergeRow:
    old_id: str
    new_id: str
    line: int = 0


@dataclass(frozen=True)
class SegmentRow:
    cust_id: str
    segment: str
    biz_date: str
    line: int = 0
