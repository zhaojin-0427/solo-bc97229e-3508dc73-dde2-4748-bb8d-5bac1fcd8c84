"""grid_parser 规则测试，直接用 stdlib unittest，无额外依赖。"""

import unittest

from bs4 import BeautifulSoup

from app.grid_parser import TableParseError, build_header_paths, parse_table


def table_of(html: str):
    return BeautifulSoup(html, "html.parser").find("table")


# 标准两级表头 + 首列 rowspan + 空单元格 + 短行
STANDARD = """
<table>
  <tr>
    <th rowspan="2">水库名称</th>
    <th colspan="2">水位</th>
    <th colspan="2">蓄水量</th>
  </tr>
  <tr><th>旬初</th><th>旬末</th><th>旬初</th><th>旬末</th></tr>
  <tr>
    <td rowspan="2">青山水库</td>
    <td>128.40</td><td>129.05</td><td>86.20</td><td>89.74</td>
  </tr>
  <tr>
    <td>129.05</td><td>129.62</td><td>41.30</td><td></td>
  </tr>
  <tr><td>碧河水库</td><td>76.10</td><td>75.88</td><td>42.05</td></tr>
</table>
"""


class StandardParseTests(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_table(table_of(STANDARD), header_rows=2)

    def test_width_from_first_row(self):
        # 首行 1 + 2 + 2 = 5 列
        self.assertEqual(self.parsed.width, 5)

    def test_header_paths_dedup_same_cell(self):
        self.assertEqual(
            self.parsed.header_paths,
            [
                "水库名称",
                "水位 / 旬初",
                "水位 / 旬末",
                "蓄水量 / 旬初",
                "蓄水量 / 旬末",
            ],
        )

    def test_rowspan_reservoir_inherited(self):
        reservoirs = {r["reservoir"] for r in self.parsed.records}
        self.assertEqual(reservoirs, {"青山水库", "碧河水库"})
        # 青山水库两条正文行各 4 条观测
        qs = [r for r in self.parsed.records if r["reservoir"] == "青山水库"]
        self.assertEqual(len(qs), 8)
        # 继承格位的来源坐标仍指向 R3C1
        for r in qs:
            if r["source_row"] == 4:
                self.assertEqual(r["source_cell"].split("C")[0], "R4")
        coord_rows = {r["source_row"] for r in qs}
        self.assertEqual(coord_rows, {3, 4})

    def test_empty_cell_kept_empty_with_coord(self):
        rec = next(
            r
            for r in self.parsed.records
            if r["source_row"] == 4 and r["header_path"] == "蓄水量 / 旬末"
        )
        self.assertEqual(rec["value"], "")
        # 该行最后一个单元格是空 td，其行内序号为 4
        self.assertEqual(rec["source_cell"], "R4C4")

    def test_short_row_hole_is_empty_without_source(self):
        rec = next(
            r
            for r in self.parsed.records
            if r["reservoir"] == "碧河水库"
            and r["header_path"] == "蓄水量 / 旬末"
        )
        self.assertEqual(rec["value"], "")
        self.assertEqual(rec["source_cell"], "")
        self.assertEqual((rec["source_row"], rec["source_col"]), (5, 5))

    def test_record_count(self):
        # 3 正文行 × 4 观测列
        self.assertEqual(len(self.parsed.records), 12)

    def test_same_path_different_columns_kept(self):
        html = """
        <table>
          <tr><th>水库</th><th>值</th><th>值</th></tr>
          <tr><td>甲</td><td>1</td><td>2</td></tr>
        </table>
        """
        parsed = parse_table(table_of(html), header_rows=1)
        self.assertEqual(parsed.header_paths, ["水库", "值", "值"])
        vals = [r["value"] for r in parsed.records]
        self.assertEqual(vals, ["1", "2"])


class HeaderRowTests(unittest.TestCase):
    def test_single_header_row(self):
        html = """
        <table>
          <tr><th>水库</th><th>水位</th><th>流量</th></tr>
          <tr><td>甲</td><td>1.0</td><td>2.0</td></tr>
        </table>
        """
        parsed = parse_table(table_of(html), header_rows=1)
        self.assertEqual(parsed.header_paths, ["水库", "水位", "流量"])
        self.assertEqual(len(parsed.records), 2)

    def test_three_header_rows(self):
        html = """
        <table>
          <tr><th rowspan="3">水库</th><th colspan="2">A</th></tr>
          <tr><th colspan="2">B</th></tr>
          <tr><th>b1</th><th>b2</th></tr>
          <tr><td>甲</td><td>1</td><td>2</td></tr>
        </table>
        """
        parsed = parse_table(table_of(html), header_rows=3)
        self.assertEqual(parsed.header_paths[1], "A / B / b1")
        self.assertEqual(parsed.header_paths[2], "A / B / b2")

    def test_header_rows_exceed_total(self):
        html = "<table><tr><th>a</th></tr></table>"
        with self.assertRaises(TableParseError):
            parse_table(table_of(html), header_rows=2)

    def test_invalid_header_count(self):
        html = "<table><tr><th>a</th><th>b</th></tr></table>"
        with self.assertRaises(TableParseError):
            parse_table(table_of(html), header_rows=4)


class StructuralErrorTests(unittest.TestCase):
    def test_cross_boundary_rowspan(self):
        html = """
        <table>
          <tr><th rowspan="3">水库</th><th colspan="2">水位</th></tr>
          <tr><th>旬初</th><th>旬末</th></tr>
          <tr><td>1</td><td>2</td></tr>
          <tr><td>甲</td><td>3</td><td>4</td></tr>
        </table>
        """
        with self.assertRaises(TableParseError) as ctx:
            parse_table(table_of(html), header_rows=2)
        self.assertIn("跨越表头与正文", str(ctx.exception))
        self.assertIn("R1C1", str(ctx.exception))

    def test_body_colspan_illegal(self):
        html = """
        <table>
          <tr><th rowspan="2">水库</th><th colspan="2">水位</th></tr>
          <tr><th>旬初</th><th>旬末</th></tr>
          <tr><td>甲</td><td colspan="2">1</td></tr>
        </table>
        """
        with self.assertRaises(TableParseError) as ctx:
            parse_table(table_of(html), header_rows=2)
        self.assertIn("正文非法合并", str(ctx.exception))
        self.assertIn("R3C2", str(ctx.exception))

    def test_body_rowspan_non_first_column(self):
        html = """
        <table>
          <tr><th>水库</th><th>水位</th><th>流量</th></tr>
          <tr><td>甲</td><td rowspan="2">1</td><td>2</td></tr>
          <tr><td>乙</td><td>3</td></tr>
        </table>
        """
        with self.assertRaises(TableParseError) as ctx:
            parse_table(table_of(html), header_rows=1)
        self.assertIn("正文非法合并", str(ctx.exception))
        self.assertIn("R2C2", str(ctx.exception))

    def test_first_column_rowspan_allowed_in_body(self):
        html = """
        <table>
          <tr><th>水库</th><th>水位</th></tr>
          <tr><td rowspan="2">甲</td><td>1</td></tr>
          <tr><td>2</td></tr>
        </table>
        """
        parsed = parse_table(table_of(html), header_rows=1)
        self.assertEqual([r["value"] for r in parsed.records], ["1", "2"])
        self.assertTrue(all(r["reservoir"] == "甲" for r in parsed.records))

    def test_horizontal_overflow(self):
        html = """
        <table>
          <tr><th>水库</th><th>水位</th></tr>
          <tr><td>甲</td><td>1</td><td>2</td></tr>
        </table>
        """
        with self.assertRaises(TableParseError) as ctx:
            parse_table(table_of(html), header_rows=1)
        self.assertIn("越界", str(ctx.exception))
        self.assertIn("表宽 2", str(ctx.exception))

    def test_overlap(self):
        # R1C2 rowspan=2 占住 R2C2；R2C1 colspan=2 从空闲的第 1 列起放，
        # 展开覆盖到第 2 列，与 R1C2 向下占据的格位重叠
        html = """
        <table>
          <tr><th>甲</th><th rowspan="2">乙</th><th>丙</th></tr>
          <tr><th colspan="2">丁</th><th>戊</th></tr>
          <tr><td>1</td><td>2</td><td>3</td></tr>
        </table>
        """
        with self.assertRaises(TableParseError) as ctx:
            parse_table(table_of(html), header_rows=2)
        self.assertIn("跨度重叠", str(ctx.exception))
        self.assertIn("R2C1", str(ctx.exception))

    def test_error_stops_before_records(self):
        html = """
        <table>
          <tr><th>水库</th><th>水位</th></tr>
          <tr><td>甲</td><td colspan="2">1</td></tr>
        </table>
        """
        with self.assertRaises(TableParseError):
            parse_table(table_of(html), header_rows=1)


class NestedTableTests(unittest.TestCase):
    def test_nested_table_rows_and_cells_ignored(self):
        html = """
        <table>
          <tr><th>水库</th><th>水位</th></tr>
          <tr>
            <td>甲</td>
            <td><table><tr><td>嵌套</td><td>内容</td></tr></table></td>
          </tr>
        </table>
        """
        parsed = parse_table(table_of(html), header_rows=1)
        self.assertEqual(parsed.width, 2)
        self.assertEqual(parsed.total_rows, 2)
        rec = parsed.records[0]
        self.assertEqual(rec["reservoir"], "甲")
        # 嵌套表格文本并入直属单元格文本，但其行/单元格不参与坐标
        self.assertIn("嵌套", rec["value"])
        self.assertEqual(rec["source_cell"], "R2C2")


class CoordinateTests(unittest.TestCase):
    def test_coords_count_from_one_and_follow_source_order(self):
        parsed = parse_table(table_of(STANDARD), header_rows=2)
        first = parsed.records[0]
        self.assertEqual(first["source_cell"], "R3C2")
        self.assertEqual(first["source_row"], 3)
        self.assertEqual(first["source_col"], 2)
        # 表头首格 rowspan=2，其坐标恒为 R1C1
        self.assertEqual(parsed.grid[0][0].coord, "R1C1")
        self.assertEqual(parsed.grid[1][0].coord, "R1C1")

    def test_header_path_helper_directly(self):
        from app.grid_parser import build_grid

        grid, width = build_grid(table_of(STANDARD), 2)
        paths = build_header_paths(grid, width, 2)
        self.assertEqual(paths[0], "水库名称")


if __name__ == "__main__":
    unittest.main(verbosity=2)
