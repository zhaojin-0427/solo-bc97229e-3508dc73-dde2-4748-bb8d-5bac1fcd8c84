# 水库公示表格采集系统（Hydro Table Collector）

把水库公示网页中的**合并单元格 HTML 表格**转换为可入库的**结构化观测记录**，
提供从 URL 抓取、表格选择、表头指定、规则校验、预览到 CSV 下载的完整 Web 流程。

## 功能与转换规则

1. **采集网页（Flask）**：输入公示页 URL，抓取并列出页面内全部 HTML 表格，
   逐个给出容错迷你预览，选择目标表格并指定**顶部 1～3 行表头**。
2. **首列固定为水库名**：正文仅首列允许 `rowspan`，跨度内向下继承水库名；
   其余正文单元格不允许任何合并。
3. **按 `rowspan` / `colspan` 展开为规则网格**：
   - 表宽 = **首行展开宽度**（首行各单元格 colspan 之和）；
   - 同一来源单元格只记录一次，表头路径不重复同一段；
   - 路径文本相同的不同列仍分别保留，不合并。
4. **完整表头路径**：沿表头各行向下拼接，分隔符为 ` / `
   （如 `水位（米） / 旬初`）。
5. **空值语义**：观测空单元格保持为空；短行缺失的网格格位同样导出空值，
   且不携带来源单元格坐标。
6. **结构错误即停**：跨度重叠、横向越界、跨越表头/正文、正文非法合并时，
   标明来源单元格位置（`R行C列`，按**原表行序及行内单元格序从 1 计数**）
   并停止转换，不产生任何记录。
7. **CSV 导出**：字段为 `水库, 表头路径, 原始文本, 来源单元格行,
   来源单元格列, 来源单元格坐标, 来源URL`，UTF-8 带 BOM，Excel 可直接打开。

## 目录结构

```
.
├── app/
│   ├── web.py                 # Flask 路由：抓取/选择/转换/下载
│   ├── grid_parser.py         # 合并单元格展开、校验、表头路径、记录生成
│   ├── templates/             # index/select/preview/error 页面
│   └── static/
│       ├── style.css
│       └── samples/
│           ├── notice.html        # 标准示例公示页（含合法 rowspan/空值/短行）
│           └── notice_error.html  # 错误示例页（跨边界 rowspan、正文非法 colspan）
├── tests/                     # 26 个 unittest 用例（解析规则 + Web 端到端）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── run_dev.sh                 # 本地无 Docker 时的开发启动脚本
```

## 方式一：Docker Compose（推荐，交付即启）

```bash
docker compose up --build
# 浏览器打开
http://localhost:8000
```

首页点击「使用标准示例页」即可离线演示完整链路；
「使用错误示例页」演示结构错误的定位与停止转换。

## 方式二：本地 Python 运行

需要 Python 3.11+：

```bash
python3 -m pip install -r requirements.txt
./run_dev.sh            # 等价于 gunicorn app.web:app ，监听 8000
# 或开发热更：
FLASK_APP=app.web flask run --host 0.0.0.0 --port 8000
```

## 演示路径

1. 打开 `http://localhost:8000/`，点击「使用标准示例页」；
2. 选择表格、表头行数保持 **2 行**，点击「转换此表格」；
3. 预览 35 条观测记录（5 条正文行 × 7 个观测列）、完整表头路径、
   来源坐标；注意青山水库的 rowspan 继承、碧河空单元格、云岭短行空值；
4. 点击「下载 CSV」获得带 BOM 的入库文件；
5. 返回首页用「错误示例页」分别转换两张表，观察带 `R行C列` 的报错与停止行为。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：表宽确定、表头去重与同路径多列保留、rowspan 继承、空值/短行、
坐标 1 基计数、四类结构错误定位、错误即停、Flask 全流程与 CSV 内容。

## 设计约束与说明

- 只解析目标 `<table>` 的直属 `<tr>/<td>/<th>`，嵌套表格不会串入。
- `rowspan=0`、非法跨度属性按浏览器约定按 1 处理；垂直方向超出表格
  底部的 rowspan 按浏览器约定裁剪，**横向**超出表宽判越界报错。
- 抓取缓存为进程内字典（TTL 30 分钟、上限 50 条），因此 Compose
  使用单 gunicorn worker + 多线程；如需多 worker 请替换为 Redis 等共享存储。
- 抓取远程页面时携带自定义 User-Agent、15 秒超时，并按响应头/内容
  推断编码，适配中文公示页常见的 GB 系编码。
