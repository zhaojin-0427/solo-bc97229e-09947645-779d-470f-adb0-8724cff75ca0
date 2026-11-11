"""身份归并入仓：直接合号映射 + 正式分群名单（SQLite 持久化）。

该子包只依赖 Python 标准库，便于在没有 Flask/Docker 的环境中直接测试。
"""

from .csv_io import (
    decode_csv_bytes,
    format_segments_csv,
    parse_merge_csv,
    parse_segments_csv,
)
from .errors import IdentityError
from .models import MergeRow, SegmentRow
from .samples import SAMPLE_MERGE_CSV, SAMPLE_SEGMENT_CSV
from .store import IdentityStore

__all__ = [
    "IdentityError",
    "IdentityStore",
    "MergeRow",
    "SegmentRow",
    "decode_csv_bytes",
    "format_segments_csv",
    "parse_merge_csv",
    "parse_segments_csv",
    "SAMPLE_MERGE_CSV",
    "SAMPLE_SEGMENT_CSV",
]
