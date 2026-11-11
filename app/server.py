"""Flask Web 入口：SQL 字段溯源 + 身份归并入仓。

路由：
  GET  /                         溯源页面（首页含身份归并入口）
  GET  /identity                 身份归并入仓页面
  POST /api/identity/merges/upload   上传 CRM 合号 CSV（整批）
  POST /api/identity/segments/upload 上传分群 CSV（整批）
  GET  /api/identity/state       映射 + 统计 + 正式名单分页（同一快照）
  GET  /api/identity/segments    正式名单分页查询
  GET  /api/identity/segments/download  下载完整正式名单 CSV
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, Response, jsonify, render_template, request

from identity import (
    IdentityError,
    IdentityStore,
    SAMPLE_MERGE_CSV,
    SAMPLE_SEGMENT_CSV,
    decode_csv_bytes,
    format_segments_csv,
    parse_merge_csv,
    parse_segments_csv,
)
from lineage import analyze
from lineage.demo_data import DEMO_SCHEMA, DEMO_SQL
from lineage.errors import LineageError

app = Flask(__name__)
# 返回可直接阅读的 UTF-8 中文错误信息（Flask 3 的 JSON provider）。
try:
    app.json.ensure_ascii = False
except AttributeError:  # pragma: no cover - 兼容极旧 Flask
    pass

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
IDENTITY_DB_PATH = (
    os.environ.get("IDENTITY_DB_PATH")
    or os.path.join(_APP_DIR, "data", "identity.db")
)
identity_store = IdentityStore(IDENTITY_DB_PATH)


# --------------------------------------------------------------------- 页面
@app.route("/")
def index():
    return render_template("index.html",
                           demo_schema=DEMO_SCHEMA,
                           demo_sql=DEMO_SQL)


@app.route("/identity")
def identity_page():
    return render_template("identity.html",
                           sample_merge=SAMPLE_MERGE_CSV,
                           sample_segments=SAMPLE_SEGMENT_CSV)


@app.route("/identity/")
@app.route("/merge")
@app.route("/merge/")
def identity_page_alias():
    return identity_page()


# --------------------------------------------------------------------- 溯源接口
@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    payload = request.get_json(silent=True) or {}
    schema_text = payload.get("schema", "")
    sql_text = payload.get("sql", "")
    try:
        result = analyze(schema_text, sql_text)
    except LineageError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover - 防御性兜底
        return jsonify({"ok": False,
                        "error": f"分析失败：{type(exc).__name__}: {exc}"}), 400
    return jsonify({"ok": True, "result": result})


# --------------------------------------------------------------------- 身份归并：工具
def _error_response(exc, status=400):
    return jsonify({
        "ok": False,
        "error": str(exc),
        "error_type": getattr(exc, "code", "error"),
    }), status


def _extract_csv_text():
    """同时支持 multipart 文件、表单 text、JSON 和原始请求体。"""
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        # 兼容常见的其他文件字段名；页面本身使用 file。
        for key in ("csv", "csv_file", "upload", "data"):
            candidate = request.files.get(key)
            if candidate is not None and candidate.filename:
                uploaded = candidate
                break
        else:
            uploaded = next(
                (item for item in request.files.values() if item and item.filename),
                None,
            )
    if uploaded is not None and uploaded.filename:
        raw = uploaded.read()
        if not raw or not raw.strip():
            raise IdentityError("上传的 CSV 文件为空，整批已拒绝", "empty")
        return decode_csv_bytes(raw)

    for value in (request.form.get("text"), request.form.get("csv")):
        if isinstance(value, str) and value.strip():
            return value

    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        for key in ("text", "csv", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value

    raw = request.get_data(cache=True)
    if raw and raw.strip():
        return decode_csv_bytes(raw)

    raise IdentityError("未收到 CSV：请上传文件或提供 CSV 文本，整批已拒绝", "empty")


def _upload_response(rows, store_call):
    try:
        result = store_call(rows)
    except IdentityError as exc:
        return _error_response(exc)
    except Exception as exc:  # pragma: no cover - 防御性兜底
        return _error_response(
            IdentityError(f"处理失败：{type(exc).__name__}: {exc}", "internal"), 500)
    payload = {"ok": True}
    payload.update(result)
    return jsonify(payload)


# --------------------------------------------------------------------- 身份归并：上传
@app.route("/api/identity/merges/upload", methods=["POST"])
@app.route("/api/identity/merge/upload", methods=["POST"])
@app.route("/api/merges/upload", methods=["POST"])
@app.route("/api/merge/upload", methods=["POST"])
def api_upload_merges():
    try:
        rows = parse_merge_csv(_extract_csv_text())
    except IdentityError as exc:
        return _error_response(exc)
    return _upload_response(rows, identity_store.upload_merges)


@app.route("/api/identity/segments/upload", methods=["POST"])
@app.route("/api/identity/segment/upload", methods=["POST"])
@app.route("/api/segments/upload", methods=["POST"])
@app.route("/api/segment/upload", methods=["POST"])
def api_upload_segments():
    try:
        rows = parse_segments_csv(_extract_csv_text())
    except IdentityError as exc:
        return _error_response(exc)
    return _upload_response(rows, identity_store.upload_segments)


# --------------------------------------------------------------------- 身份归并：查询
@app.route("/api/identity/state", methods=["GET"])
@app.route("/api/identity/status", methods=["GET"])
@app.route("/api/identity", methods=["GET"])
def api_identity_state():
    try:
        state = identity_store.get_state(
            request.args.get("page", 1),
            request.args.get("page_size", 500),
        )
    except IdentityError as exc:
        return _error_response(exc)
    except Exception as exc:  # pragma: no cover
        return _error_response(
            IdentityError(f"读取失败：{type(exc).__name__}: {exc}", "internal"), 500)
    return jsonify({"ok": True, **state})


@app.route("/api/identity/merges", methods=["GET"])
@app.route("/api/identity/mappings", methods=["GET"])
@app.route("/api/merges", methods=["GET"])
def api_list_merges():
    try:
        merges = identity_store.list_merges()
    except IdentityError as exc:
        return _error_response(exc)
    except Exception as exc:  # pragma: no cover
        return _error_response(
            IdentityError(f"读取失败：{type(exc).__name__}: {exc}", "internal"), 500)
    return jsonify({"ok": True, "merges": merges, "count": len(merges)})


@app.route("/api/identity/segments", methods=["GET"])
@app.route("/api/segments", methods=["GET"])
def api_list_segments():
    try:
        page = identity_store.list_segments(
            request.args.get("page", 1),
            request.args.get("page_size", 500),
        )
    except IdentityError as exc:
        return _error_response(exc)
    except Exception as exc:  # pragma: no cover
        return _error_response(
            IdentityError(f"读取失败：{type(exc).__name__}: {exc}", "internal"), 500)
    return jsonify({"ok": True, **page})


def _csv_response(body, filename):
    response = Response(body, mimetype="text/csv")
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/identity/segments/download", methods=["GET"])
@app.route("/api/identity/segments/export", methods=["GET"])
@app.route("/api/identity/segments/export.csv", methods=["GET"])
@app.route("/api/identity/segments.csv", methods=["GET"])
@app.route("/api/segments/download", methods=["GET"])
@app.route("/api/segments/export.csv", methods=["GET"])
@app.route("/api/segments.csv", methods=["GET"])
@app.route("/export.csv", methods=["GET"])
@app.route("/download", methods=["GET"])
def api_download_segments():
    try:
        rows = identity_store.export_segments()
    except IdentityError as exc:
        return _error_response(exc)
    except Exception as exc:  # pragma: no cover
        return _error_response(
            IdentityError(f"导出失败：{type(exc).__name__}: {exc}", "internal"), 500)
    return _csv_response(format_segments_csv(rows), "customer_segments.csv")


@app.route("/api/identity/sample/merges.csv", methods=["GET"])
def api_sample_merges():
    return _csv_response(SAMPLE_MERGE_CSV, "sample_merges.csv")


@app.route("/api/identity/sample/segments.csv", methods=["GET"])
def api_sample_segments():
    return _csv_response(SAMPLE_SEGMENT_CSV, "sample_segments.csv")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
