"""并发会话回归测试。

历史缺陷：抓取/转换会话 key 由「毫秒时间戳 + 当前缓存长度」拼成。多个
线程在同一毫秒提交不同 URL 时，会在插入缓存前算出完全相同的 key，
后写入的页面覆盖先写入的页面，导致选择页、转换结果和 CSV 串到别的 URL。

本测试用固定时钟 + 屏障让所有线程在 ``_cache_put`` 前算出 key 并同时
放行，从而可重复地复现该竞争条件；会话 key 改为全局唯一令牌后，每条
请求都应始终拿回属于自己的页面、记录与来源 URL。
"""

from __future__ import annotations

import csv
import io
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import web as web_module
from app.web import app


# 每个 URL 对应一张内容唯一、彼此可区分的表。
PAGES = {}
for _n in range(4):
    _url = f"http://source-{_n}.example.gov.cn/notice"
    PAGES[_url] = f"""
    <html><body>
      <table>
        <tr><th>水库</th><th>指标{_n}</th></tr>
        <tr><td>水库{_n}</td><td>数值{_n}</td></tr>
      </table>
    </body></html>
    """


class FrozenClock:
    """只替换 app.web 里的 time，不影响进程中的真实时钟。"""

    def __init__(self, value: float) -> None:
        self.value = value

    def time(self) -> float:
        return self.value


def _extract_hidden_key(html: str) -> str:
    match = re.search(r'name="cache_key" value="([^"]+)"', html)
    if not match:
        raise AssertionError("选择页缺少 cache_key 隐藏字段")
    return match.group(1)


def _extract_result_key(html: str) -> str:
    match = re.search(r'/download\?key=([^"&]+)', html)
    if not match:
        raise AssertionError("预览页缺少下载链接")
    return match.group(1)


class ConcurrentSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        app.testing = True
        with web_module._FETCH_CACHE_LOCK:
            web_module._FETCH_CACHE.clear()

    def _run_flow(self, url: str) -> tuple[str, str]:
        """单线程完整流程：抓取 → 选择 → 转换 → 下载并校验内容。"""
        client = app.test_client()
        n = int(url.split("source-")[1].split(".")[0])

        fetch = client.post("/fetch", data={"url": url})
        self.assertEqual(fetch.status_code, 200)
        select_html = fetch.data.decode()
        self.assertIn(url, select_html)
        cache_key = _extract_hidden_key(select_html)

        parse = client.post(
            "/parse",
            data={"cache_key": cache_key, "table_index": 0, "header_rows": 1},
        )
        self.assertEqual(parse.status_code, 200)
        preview_html = parse.data.decode()
        self.assertIn(url, preview_html)
        self.assertIn(f"指标{n}", preview_html)
        self.assertIn(f"水库{n}", preview_html)
        result_key = _extract_result_key(preview_html)

        download = client.get(f"/download?key={result_key}")
        self.assertEqual(download.status_code, 200)
        self.assertTrue(download.data.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(download.data.decode("utf-8-sig"))))

        self.assertEqual(rows[0][0], "水库")
        self.assertEqual(
            rows[1],
            [
                f"水库{n}",
                f"指标{n}",
                f"数值{n}",
                "2",
                "2",
                "R2C2",
                url,
            ],
        )
        self.assertEqual(len(rows), 2)

        # 缓存内的会话也必须仍指向本 URL，而不是被并发请求覆盖。
        with web_module._FETCH_CACHE_LOCK:
            cached = web_module._FETCH_CACHE[cache_key]
        self.assertEqual(cached["url"], url)
        self.assertIn(f"指标{n}", cached["html"])

        return cache_key, result_key

    def test_same_millisecond_concurrent_pages_stay_isolated(self):
        """所有 URL 在同一毫秒提交，各自的选择/结果/CSV 不得互相串用。"""
        urls = list(PAGES)
        workers = 8  # 每个 URL 提交两次，制造更强的并发压力
        tasks = [urls[i % len(urls)] for i in range(workers)]

        barrier = threading.Barrier(len(tasks))
        original_put = web_module._cache_put

        def synced_put(key, payload):
            # 到这里时调用方已经算好 key，屏障保证旧实现算出相同 key。
            barrier.wait(timeout=20)
            return original_put(key, payload)

        with patch("app.web.time", FrozenClock(1234567890.0)), patch(
            "app.web._fetch_html", side_effect=lambda url: PAGES[url]
        ), patch("app.web._cache_put", side_effect=synced_put):
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(self._run_flow, tasks))

        fetch_keys = [item[0] for item in results]
        result_keys = [item[1] for item in results]
        # 每个会话（包括同一 URL 的并发会话）都必须有独立 key。
        self.assertEqual(len(set(fetch_keys)), workers)
        self.assertEqual(len(set(result_keys)), workers)

    def test_cache_key_tokens_unique_across_threads(self):
        """key 生成器本身不得依赖时间戳/缓存长度。"""
        count = 64
        barrier = threading.Barrier(count)
        produced: list[str] = []
        lock = threading.Lock()

        def make_key(i: int) -> None:
            barrier.wait(timeout=20)
            key = web_module._new_cache_key(f"k{i}")
            with lock:
                produced.append(key)

        with ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(make_key, range(count)))

        self.assertEqual(len(produced), count)
        self.assertEqual(len(set(produced)), count)


if __name__ == "__main__":
    unittest.main(verbosity=2)
