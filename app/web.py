"""Flask 采集入口：抓取公示页 → 选择表格/表头行数 → 预览 → 下载 CSV。"""

from __future__ import annotations

import csv
import io
import time
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup
from flask import (
    Flask,
    Response,
    abort,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from .grid_parser import (
    TableParseError,
    lenient_matrix,
    parse_table,
)

app = Flask(__name__)

# 演示用进程内缓存（单 worker 足够）：key -> {"html", "url", "tables"}
_FETCH_CACHE: dict[str, dict] = {}
_FETCH_TTL = 30 * 60  # 30 分钟
_FETCH_MAX = 50

SAMPLE_URL = "/sample/notice.html"
SAMPLE_ERROR_URL = "/sample/notice_error.html"
USER_AGENT = "HydroTableCollector/1.0 (+reservoir bulletin archiver)"


def _cache_put(key: str, payload: dict) -> None:
    now = time.time()
    expired = [k for k, v in _FETCH_CACHE.items() if now - v["ts"] > _FETCH_TTL]
    for k in expired:
        _FETCH_CACHE.pop(k, None)
    while len(_FETCH_CACHE) >= _FETCH_MAX:
        _FETCH_CACHE.pop(next(iter(_FETCH_CACHE)))
    payload["ts"] = now
    _FETCH_CACHE[key] = payload


def _cache_get(key: str) -> Optional[dict]:
    item = _FETCH_CACHE.get(key)
    if item is None:
        return None
    if time.time() - item["ts"] > _FETCH_TTL:
        _FETCH_CACHE.pop(key, None)
        return None
    return item


def _is_local_sample(url: str) -> bool:
    return url.startswith("/sample/")


def _normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise TableParseError("请输入公示页 URL。")
    if url.startswith(("http://", "https://", "/")):
        return url
    return "https://" + url


def _fetch_html(url: str) -> str:
    if _is_local_sample(url):
        # 本地示例：直接读静态文件，保证离线可演示
        path = url[len("/sample/"):]
        safe = path.replace("/", "_")
        full = app.static_folder + "/samples/" + safe
        try:
            with open(full, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise TableParseError(f"本地示例页不存在：{url}（{exc}）")

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise TableParseError(f"抓取页面失败：{exc}")

    # requests 按响应头推断编码，推断失败时回退 utf-8
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def _parse_tables(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    summaries = []
    for idx, table in enumerate(soup.find_all("table")):
        rows = table.find_all("tr")
        cell_count = len(table.find_all(["td", "th"]))
        caption = table.find("caption")
        caption_text = caption.get_text(strip=True) if caption else ""
        # 容错预览：默认按 2 行表头展开
        grid, width = lenient_matrix(table, header_rows=min(2, max(1, len(rows))))
        preview = []
        for line in grid[: 6]:
            preview.append([p.text if p else "" for p in line[: 8]])
        summaries.append(
            {
                "index": idx,
                "n_rows": len(rows),
                "n_cells": cell_count,
                "width": width,
                "caption": caption_text,
                "preview": preview,
            }
        )
    return summaries


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template(
        "index.html",
        sample_url=SAMPLE_URL,
        sample_error_url=SAMPLE_ERROR_URL,
    )


@app.post("/fetch")
def fetch():
    raw_url = request.form.get("url", "")
    try:
        url = _normalize_url(raw_url)
        html = _fetch_html(url)
    except TableParseError as exc:
        return render_template("error.html", message=str(exc), stage="抓取"), 400

    soup = BeautifulSoup(html, "html.parser")
    tables_html = soup.find_all("table")
    if not tables_html:
        return render_template(
            "error.html",
            message="该页面没有解析到任何 <table> 表格，请确认 URL 指向公示页。",
            stage="解析",
        ), 400

    summaries = _parse_tables(html)
    cache_key = f"f{int(time.time() * 1000)}-{len(_FETCH_CACHE)}"
    _cache_put(
        cache_key,
        {"html": html, "url": url, "tables": summaries},
    )
    return render_template(
        "select.html",
        cache_key=cache_key,
        url=url,
        tables=summaries,
    )


@app.post("/parse")
def parse():
    cache_key = request.form.get("cache_key", "")
    cached = _cache_get(cache_key)
    if cached is None:
        return render_template(
            "error.html",
            message="会话已过期或缺失，请重新输入 URL 抓取。",
            stage="选择",
        ), 400

    try:
        table_index = int(request.form.get("table_index", ""))
        header_rows = int(request.form.get("header_rows", ""))
    except (TypeError, ValueError):
        return render_template(
            "error.html", message="表格编号或表头行数无效。", stage="选择"
        ), 400

    soup = BeautifulSoup(cached["html"], "html.parser")
    tables = soup.find_all("table")
    if not 0 <= table_index < len(tables):
        abort(400, "表格编号超出范围。")

    try:
        parsed = parse_table(
            tables[table_index], header_rows, source_url=cached["url"]
        )
    except TableParseError as exc:
        return render_template(
            "error.html", message=str(exc), stage="转换", stopped=True
        ), 422

    result_key = f"r{int(time.time() * 1000)}-{len(_FETCH_CACHE)}"
    _cache_put(
        result_key,
        {
            "records": parsed.records,
            "url": cached["url"],
            "header_paths": parsed.header_paths,
            "width": parsed.width,
            "total_rows": parsed.total_rows,
            "header_rows": parsed.header_rows,
        },
    )
    return render_template(
        "preview.html",
        result_key=result_key,
        url=cached["url"],
        parsed=parsed,
        records=parsed.records,
    )


@app.get("/download")
def download():
    result_key = request.args.get("key", "")
    cached = _cache_get(result_key)
    if cached is None:
        return render_template(
            "error.html",
            message="下载会话已过期，请重新抓取并转换。",
            stage="下载",
        ), 400

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "水库",
            "表头路径",
            "原始文本",
            "来源单元格行",
            "来源单元格列",
            "来源单元格坐标",
            "来源URL",
        ]
    )
    for rec in cached["records"]:
        writer.writerow(
            [
                rec["reservoir"],
                rec["header_path"],
                rec["value"],
                rec["source_row"],
                rec["source_col"],
                rec["source_cell"],
                rec["source_url"],
            ]
        )

    # utf-8-sig 便于 Excel 直接打开中文
    payload = buf.getvalue().encode("utf-8-sig")
    fname = "reservoir_observations.csv"
    quoted = urllib.parse.quote(fname)
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f"attachment; filename={fname}; filename*=UTF-8''{quoted}"
        },
    )


@app.get("/sample/<path:filename>")
def sample(filename: str):
    return send_from_directory(
        app.static_folder + "/samples", filename, mimetype="text/html"
    )


@app.errorhandler(404)
def not_found(_err):
    return render_template("error.html", message="页面不存在。", stage="路由"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
