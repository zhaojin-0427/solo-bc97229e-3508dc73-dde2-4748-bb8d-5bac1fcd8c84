"""HTML 合并单元格表格解析核心。

规则摘要
========
* 只解析 ``<table>`` 的直属行（``<tr>``），不会进入嵌套表格。
* 按 ``rowspan`` / ``colspan`` 把单元格展开到规则网格中；网格宽度
  （表宽）由**第一行展开后的宽度**决定。
* 表头为顶部 1~3 行；表头单元格按网格列生成完整表头路径，同一来源
  单元格只记录一次（路径中不会重复同一段），路径相同的不同列仍各自
  保留。
* 正文首列固定为水库名，只允许首列出现 ``rowspan``（跨正文行继承
  水库名）；其余正文单元格不允许任何合并。
* 出现跨度重叠、越界、跨越表头/正文、正文非法合并时抛出
  :class:`TableParseError`，错误信息标明来源单元格在原表中的位置，
  转换立即停止。
* 所有行列坐标均按**原表行序、行内单元格序**从 1 计数；被合并覆盖
  的网格格位继承来源单元格的坐标。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from bs4 import Tag

CELL_TAGS = ("td", "th")


class TableParseError(Exception):
    """表格结构非法，无法继续转换。"""


@dataclass
class Placement:
    """一个网格格位的归属信息。"""

    source_row: int  # 来源单元格所在原表行（1 基）
    source_col: int  # 来源单元格在该行单元格序（1 基）
    text: str
    rowspan: int
    colspan: int
    cell_tag: str  # "td" / "th"

    @property
    def coord(self) -> str:
        """形如 ``R2C3`` 的来源坐标。"""
        return f"R{self.source_row}C{self.source_col}"


@dataclass
class ParsedTable:
    """解析产物。"""

    width: int
    total_rows: int
    header_rows: int
    header_paths: list[str]
    grid: list[list[Optional[Placement]]]
    records: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _direct_rows(table: Tag) -> list[Tag]:
    """取表格的直属 ``<tr>``，排除嵌套表格的行。"""
    rows: list[Tag] = []
    for tr in table.find_all("tr"):
        parent = tr.parent
        # tr 的祖先链上第一个 table 必须是当前 table
        while parent is not None and parent.name != "table":
            parent = parent.parent
        if parent is table:
            rows.append(tr)
    return rows


def _direct_cells(tr: Tag) -> list[Tag]:
    """取一行的直属单元格（td/th），排除嵌套表格中的单元格。"""
    cells: list[Tag] = []
    for cell in tr.find_all(CELL_TAGS):
        # 向上找最近的 tr，嵌套表格中的单元格其最近 tr 不是本行
        node = cell.parent
        while node is not None and node.name != "tr":
            node = node.parent
        if node is tr:
            cells.append(cell)
    return cells


def _span(value) -> int:
    """解析 rowspan/colspan 属性；非法、0 一律按 1 处理（浏览器约定）。"""
    if value is None:
        return 1
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return 1
    return n if n >= 1 else 1


def _cell_text(cell: Tag) -> str:
    """提取单元格文本，折叠空白，去掉首尾空白。"""
    return " ".join(cell.get_text().split())


# ---------------------------------------------------------------------------
# 网格展开
# ---------------------------------------------------------------------------

def build_grid(
    table: Tag,
    header_rows: int,
    *,
    strict: bool = True,
) -> tuple[list[list[Optional[Placement]]], int]:
    """把表格展开为规则网格。

    :param strict: 严格模式下任何结构问题都抛出 :class:`TableParseError`；
        非严格模式仅用于页面上的容错预览，跳过问题单元格。
    :returns: ``(grid, width)``，grid 为二维列表，空位为 ``None``。
    """
    if not isinstance(header_rows, int) or not (1 <= header_rows <= 3):
        raise TableParseError("表头行数必须是 1、2 或 3。")

    rows = _direct_rows(table)
    if not rows:
        raise TableParseError("该表格没有任何数据行（<tr>）。")
    if header_rows > len(rows):
        raise TableParseError(
            f"指定表头 {header_rows} 行，但表格实际只有 {len(rows)} 行。"
        )

    # 表宽 = 首行展开宽度（首行各单元格 colspan 之和）
    first_cells = _direct_cells(rows[0])
    if not first_cells:
        raise TableParseError("表格首行为空，无法确定表宽。")
    width = sum(_span(c.get("colspan")) for c in first_cells)
    grid: list[list[Optional[Placement]]] = [
        [None] * width for _ in rows
    ]

    for r_idx, tr in enumerate(rows, start=1):
        cells = _direct_cells(tr)
        c_cursor = 0  # 下一个可放置的网格列

        for c_idx, cell in enumerate(cells, start=1):
            while c_cursor < width and grid[r_idx - 1][c_cursor] is not None:
                c_cursor += 1

            rs = _span(cell.get("rowspan"))
            cs = _span(cell.get("colspan"))
            start_col = c_cursor
            end_col = start_col + cs - 1  # 网格列（0 基）

            # 越界（水平超出表宽）
            if end_col >= width:
                msg = (
                    f"越界：来源单元格 R{r_idx}C{c_idx}"
                    f"（{cell.name}，colspan={cs}）"
                    f"横向展开到第 {end_col + 1} 列，"
                    f"超过表宽 {width} 列（表宽由首行展开宽度决定）。"
                )
                if strict:
                    raise TableParseError(msg)
                continue

            end_row = r_idx - 1 + rs - 1  # 网格行（0 基）

            # 跨越表头/正文边界
            crosses_boundary = (
                r_idx <= header_rows and end_row + 1 > header_rows
            )
            if crosses_boundary:
                msg = (
                    f"跨越表头与正文：来源单元格 R{r_idx}C{c_idx}"
                    f"（{cell.name}，rowspan={rs}）起于表头第 {r_idx} 行，"
                    f"向下延伸到第 {end_row + 1} 行，越过表头/正文分界"
                    f"（第 {header_rows} 行之后为正文）。"
                )
                if strict:
                    raise TableParseError(msg)
                continue

            # 正文非法合并：仅首列允许 rowspan，其余一律禁止合并
            in_body = r_idx > header_rows
            if in_body:
                if cs > 1 or (rs > 1 and start_col != 0):
                    msg = (
                        f"正文非法合并：来源单元格 R{r_idx}C{c_idx}"
                        f"（{cell.name}，rowspan={rs}，colspan={cs}）"
                        f"位于正文第 {r_idx - header_rows} 行第 {start_col + 1} 列；"
                        "正文仅首列（水库名列）允许 rowspan，"
                        "其余单元格必须为普通单元格。"
                    )
                    if strict:
                        raise TableParseError(msg)
                    continue

            placement = Placement(
                source_row=r_idx,
                source_col=c_idx,
                text=_cell_text(cell),
                rowspan=rs,
                colspan=cs,
                cell_tag=cell.name,
            )

            # 重叠检测 & 落格（垂直超出表格底部按浏览器约定裁剪）
            overlap = False
            for rr in range(r_idx - 1, min(end_row + 1, len(rows))):
                for cc in range(start_col, end_col + 1):
                    if grid[rr][cc] is not None:
                        overlap = True
                        break
                if overlap:
                    break
            if overlap:
                owner = grid[rr][cc]  # type: ignore[union-attr]
                msg = (
                    f"跨度重叠：来源单元格 R{r_idx}C{c_idx}"
                    f"（{cell.name}，rowspan={rs}，colspan={cs}）"
                    f"展开后与 R{owner.source_row}C{owner.source_col}"
                    f"（{owner.cell_tag}）在网格第 {rr + 1} 行第 {cc + 1} 列重叠。"
                )
                if strict:
                    raise TableParseError(msg)
                continue

            for rr in range(r_idx - 1, min(end_row + 1, len(rows))):
                for cc in range(start_col, end_col + 1):
                    grid[rr][cc] = placement

            c_cursor = end_col + 1

    return grid, width


# ---------------------------------------------------------------------------
# 表头路径
# ---------------------------------------------------------------------------

def build_header_paths(
    grid: list[list[Optional[Placement]]],
    width: int,
    header_rows: int,
) -> list[str]:
    """为每一列生成完整表头路径。

    沿表头各行向下收集来源文本；同一来源单元格（同一来源坐标）只记
    一次，因此被 rowspan 覆盖的格位不会在路径中产生重复段。不同列
    即使拼出的路径文本相同，也分别保留。
    """
    paths: list[str] = []
    for col in range(width):
        segments: list[str] = []
        seen_coords: set[str] = set()
        for row in range(header_rows):
            p = grid[row][col]
            if p is None:
                continue
            if p.coord in seen_coords:
                continue
            seen_coords.add(p.coord)
            if p.text:
                segments.append(p.text)
        paths.append(" / ".join(segments))
    return paths


# ---------------------------------------------------------------------------
# 正文记录
# ---------------------------------------------------------------------------

def extract_records(
    grid: list[list[Optional[Placement]]],
    width: int,
    header_rows: int,
    header_paths: list[str],
    source_url: str = "",
) -> list[dict]:
    """把正文展开为可入库的观测记录（每行 × 每个观测列一条）。"""
    records: list[dict] = []
    if width <= 1:
        return records

    body = grid[header_rows:]
    for body_idx, row_cells in enumerate(body, start=1):
        table_row = header_rows + body_idx
        reservoir_p = row_cells[0]
        reservoir = reservoir_p.text if reservoir_p is not None else ""
        reservoir_coord = reservoir_p.coord if reservoir_p is not None else ""

        for col in range(1, width):
            p = row_cells[col]
            records.append(
                {
                    "reservoir": reservoir,
                    "header_path": header_paths[col],
                    "value": p.text if p is not None else "",
                    "source_row": table_row,
                    "source_col": col + 1,
                    "source_cell": p.coord if p is not None else "",
                    "source_url": source_url,
                }
            )
    return records


def parse_table(
    table: Tag,
    header_rows: int,
    source_url: str = "",
) -> ParsedTable:
    """完整解析流程：展开网格 → 表头路径 → 正文记录。"""
    grid, width = build_grid(table, header_rows, strict=True)
    rows = _direct_rows(table)
    header_paths = build_header_paths(grid, width, header_rows)
    records = extract_records(
        grid, width, header_rows, header_paths, source_url
    )
    return ParsedTable(
        width=width,
        total_rows=len(rows),
        header_rows=header_rows,
        header_paths=header_paths,
        grid=grid,
        records=records,
    )


def lenient_matrix(
    table: Tag, header_rows: int
) -> tuple[list[list[Optional[Placement]]], int]:
    """容错展开，供选择表格页面的迷你预览使用，不抛结构异常。"""
    try:
        return build_grid(table, header_rows, strict=False)
    except TableParseError:
        rows = _direct_rows(table)
        grid: list[list[Optional[Placement]]] = []
        width = 1
        for r_idx, tr in enumerate(rows, start=1):
            line: list[Optional[Placement]] = []
            for c_idx, cell in enumerate(_direct_cells(tr), start=1):
                line.append(
                    Placement(
                        source_row=r_idx,
                        source_col=c_idx,
                        text=_cell_text(cell),
                        rowspan=_span(cell.get("rowspan")),
                        colspan=_span(cell.get("colspan")),
                        cell_tag=cell.name,
                    )
                )
            width = max(width, len(line))
            grid.append(line)
        for line in grid:
            line += [None] * (width - len(line))
        return grid, width
