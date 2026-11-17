#!/usr/bin/env python3
"""tablekit.py 的完整单元测试。

运行：
    python3 -m unittest -v
"""

import base64
import contextlib
import csv
import io
import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import tablekit as tk


# --------------------------------------------------------------------------
# 测试辅助
# --------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _csv_text(rows, header=None) -> str:
    """把二维列表转成 CSV 文本（无 BOM）。"""
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    if header is not None:
        w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def _csv_bytes(text: str, encoding: str = "utf-8") -> bytes:
    return text.encode(encoding)


class TempDirMixin:
    """每个测试用独立的临时目录（不使用 chdir）。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()


# --------------------------------------------------------------------------
# 1. 读取 / 多编码
# --------------------------------------------------------------------------

class TestReadEncodings(unittest.TestCase):
    def test_utf8_bom(self):
        data = "\ufeffid,name\n1,张三\n".encode("utf-8")
        headers, rows = tk.read_table_bytes(data, "a.csv")
        self.assertEqual(headers, ["id", "name"])
        self.assertEqual(rows, [{"id": "1", "name": "张三"}])

    def test_plain_utf8(self):
        headers, rows = tk.read_table_bytes("a,b\n1,2\n".encode("utf-8"), "a.csv")
        self.assertEqual(headers, ["a", "b"])
        self.assertEqual(rows, [{"a": "1", "b": "2"}])

    def test_gbk(self):
        data = "姓名,城市\n李四,北京\n".encode("gbk")
        headers, rows = tk.read_table_bytes(data, "gbk.csv")
        self.assertEqual(headers, ["姓名", "城市"])
        self.assertEqual(rows[0], {"姓名": "李四", "城市": "北京"})

    def test_gb18030_only_char(self):
        # 该字符无法用 gbk 表示，必须由 gb18030 兜底
        text = "名称\n䶮\n"
        data = text.encode("gb18030")
        with self.assertRaises(UnicodeDecodeError):
            data.decode("gbk")
        headers, rows = tk.read_table_bytes(data, "gb.csv")
        self.assertEqual(rows[0]["名称"], "䶮")

    def test_encoding_priority_utf8_wins(self):
        # UTF-8 中文与 GBK 都能解码，但优先 UTF-8，不得出现乱码
        data = "名称\n中文\n".encode("utf-8")
        _, rows = tk.read_table_bytes(data, "x.csv")
        self.assertEqual(rows[0]["名称"], "中文")

    def test_undecodable_bytes_never_raise(self):
        data = b"\xff\xfe\x00bad,a\n1,2\n"
        headers, rows = tk.read_table_bytes(data, "bad.csv")
        self.assertIsInstance(headers, list)

    def test_tsv(self):
        data = "a\tb\n1\t2\n".encode("utf-8")
        headers, rows = tk.read_table_bytes(data, "t.tsv")
        self.assertEqual(headers, ["a", "b"])
        self.assertEqual(rows, [{"a": "1", "b": "2"}])

    def test_tab_extension(self):
        headers, _ = tk.read_table_bytes(b"a\tb\n1\t2\n", "t.tab")
        self.assertEqual(headers, ["a", "b"])

    def test_semicolon_sniff(self):
        headers, rows = tk.read_table_bytes(b"a;b;c\n1;2;3\n4;5;6\n", "s.csv")
        self.assertEqual(headers, ["a", "b", "c"])
        self.assertEqual(rows[1], {"a": "4", "b": "5", "c": "6"})

    def test_pipe_sniff(self):
        headers, _ = tk.read_table_bytes(b"a|b\n1|2\n", "p.csv")
        self.assertEqual(headers, ["a", "b"])

    def test_empty_bytes(self):
        self.assertEqual(tk.read_table_bytes(b"", "e.csv"), ([], []))

    def test_header_only(self):
        self.assertEqual(tk.read_table_bytes(b"a,b\n", "e.csv"), (["a", "b"], []))

    def test_blank_rows_dropped_short_rows_padded(self):
        data = "a,b,c\n1,2\n,,\n3,4,5,6\n".encode("utf-8")
        headers, rows = tk.read_table_bytes(data, "r.csv")
        self.assertEqual(headers, ["a", "b", "c"])
        self.assertEqual(rows, [
            {"a": "1", "b": "2", "c": ""},
            {"a": "3", "b": "4", "c": "5"},
        ])

    def test_whitespace_only_row_dropped(self):
        data = "a,b\n1,2\n  ,  \n".encode("utf-8")
        _, rows = tk.read_table_bytes(data, "w.csv")
        self.assertEqual(len(rows), 1)

    def test_quoted_field_with_comma_and_newline(self):
        data = 'a,b\n"x,1","line1\nline2"\n'.encode("utf-8")
        _, rows = tk.read_table_bytes(data, "q.csv")
        self.assertEqual(rows[0]["a"], "x,1")
        self.assertEqual(rows[0]["b"], "line1\nline2")

    def test_crlf(self):
        _, rows = tk.read_table_bytes(b"a,b\r\n1,2\r\n", "c.csv")
        self.assertEqual(rows, [{"a": "1", "b": "2"}])

    def test_header_whitespace_trimmed(self):
        headers, _ = tk.read_table_bytes(b" a , b \n1,2\n", "h.csv")
        self.assertEqual(headers, ["a", "b"])


class TestDedupeHeaders(unittest.TestCase):
    def test_sequence(self):
        self.assertEqual(
            tk._dedupe_headers(["x", "x", "x", "", "x"]),
            ["x", "x_1", "x_2", "column_4", "x_3"],
        )

    def test_leading_empty_name(self):
        self.assertEqual(tk._dedupe_headers(["", ""]), ["column_1", "column_2"])

    def test_empty_list(self):
        self.assertEqual(tk._dedupe_headers([]), [])

    def test_duplicate_headers_in_file(self):
        headers, rows = tk.read_table_bytes(b"a,a,,a\n1,2,3,4\n", "d.csv")
        self.assertEqual(headers, ["a", "a_1", "column_3", "a_2"])
        self.assertEqual(rows[0], {"a": "1", "a_1": "2", "column_3": "3", "a_2": "4"})


class TestXlsx(unittest.TestCase):
    def test_xlsx_roundtrip_or_error(self):
        if tk.openpyxl is None:
            with self.assertRaises(RuntimeError):
                tk.read_table_bytes(b"anything", "t.xlsx")
            return
        wb = tk.openpyxl.Workbook()
        ws = wb.active
        ws.append(["id", "name", None])
        ws.append([1, "张三", None])
        ws.append([None, None, None])
        buf = io.BytesIO()
        wb.save(buf)
        headers, rows = tk.read_table_bytes(buf.getvalue(), "t.xlsx")
        self.assertEqual(headers, ["id", "name", "column_3"])
        self.assertEqual(rows[0]["id"], "1")
        self.assertEqual(rows[0]["name"], "张三")

    def test_xlsx_extension_case_insensitive(self):
        if tk.openpyxl is None:
            with self.assertRaises(RuntimeError):
                tk.read_table_bytes(b"x", "SHOUT.XLSX")
            return
        wb = tk.openpyxl.Workbook()
        wb.active.append(["a"])
        buf = io.BytesIO()
        wb.save(buf)
        self.assertEqual(tk.read_table_bytes(buf.getvalue(), "S.XLSM")[0], ["a"])

    def test_is_xlsx_helper(self):
        self.assertTrue(tk._is_xlsx("a.XLSX"))
        self.assertTrue(tk._is_xlsx("a.xlsm"))
        self.assertFalse(tk._is_xlsx("a.csv"))

    def test_xlsx_empty_workbook(self):
        if tk.openpyxl is None:
            self.skipTest("openpyxl 未安装")
        # 完全没有写入任何单元格 -> 无表头行
        wb = tk.openpyxl.Workbook()
        buf = io.BytesIO()
        wb.save(buf)
        self.assertEqual(tk.read_table_bytes(buf.getvalue(), "e.xlsx"), ([], []))

    def test_xlsx_header_only(self):
        if tk.openpyxl is None:
            self.skipTest("openpyxl 未安装")
        wb = tk.openpyxl.Workbook()
        wb.active.append(["a", "b"])
        buf = io.BytesIO()
        wb.save(buf)
        self.assertEqual(tk.read_table_bytes(buf.getvalue(), "h.xlsx"), (["a", "b"], []))


# --------------------------------------------------------------------------
# 2. 写出
# --------------------------------------------------------------------------

class TestWriteCsvBytes(unittest.TestCase):
    def test_bom_crlf_column_order(self):
        out = tk.write_csv_bytes(["a", "b"], [{"b": "2", "a": "1"}, {"a": "3"}])
        self.assertTrue(out.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(out.decode("utf-8-sig"), "a,b\r\n1,2\r\n3,\r\n")

    def test_empty(self):
        self.assertEqual(tk.write_csv_bytes([], []), b"\xef\xbb\xbf\r\n")

    def test_written_bytes_read_back(self):
        headers = ["名称", "值"]
        rows = [{"名称": "甲", "值": "1,2"}]
        data = tk.write_csv_bytes(headers, rows)
        self.assertEqual(tk.read_table_bytes(data, "x.csv"), (headers, rows))

    def test_extra_keys_ignored(self):
        out = tk.write_csv_bytes(["a"], [{"a": "1", "z": "9"}])
        self.assertEqual(out.decode("utf-8-sig"), "a\r\n1\r\n")


# --------------------------------------------------------------------------
# 3. 合并
# --------------------------------------------------------------------------

class TestMergeTables(unittest.TestCase):
    def test_union_and_source_column(self):
        headers, rows = tk.merge_tables([
            ("a.csv", ["id", "n"], [{"id": "1", "n": "x"}]),
            ("b.csv", ["id", "c"], [{"id": "2", "c": "y"}]),
        ])
        self.assertEqual(headers, ["_source_file", "id", "n", "c"])
        self.assertEqual(rows, [
            {"_source_file": "a.csv", "id": "1", "n": "x", "c": ""},
            {"_source_file": "b.csv", "id": "2", "n": "", "c": "y"},
        ])

    def test_no_source_col(self):
        headers, rows = tk.merge_tables([("a.csv", ["id"], [{"id": "1"}])], add_source_col=False)
        self.assertEqual(headers, ["id"])
        self.assertEqual(rows, [{"id": "1"}])

    def test_empty_input(self):
        self.assertEqual(tk.merge_tables([], add_source_col=False), ([], []))
        self.assertEqual(tk.merge_tables([], add_source_col=True), (["_source_file"], []))

    def test_duplicate_columns_across_files_merged_once(self):
        headers, _ = tk.merge_tables([
            ("a", ["x", "y"], []),
            ("b", ["y", "z"], []),
        ], add_source_col=False)
        self.assertEqual(headers, ["x", "y", "z"])

    def test_row_order_preserved(self):
        _, rows = tk.merge_tables([
            ("a", ["k"], [{"k": "1"}, {"k": "2"}]),
            ("b", ["k"], [{"k": "3"}]),
        ], add_source_col=False)
        self.assertEqual([r["k"] for r in rows], ["1", "2", "3"])

    def test_file_without_rows(self):
        _, rows = tk.merge_tables([("a", ["k"], []), ("b", ["k"], [{"k": "1"}])],
                                  add_source_col=False)
        self.assertEqual(len(rows), 1)


# --------------------------------------------------------------------------
# 4. 清洗 / 日期标准化
# --------------------------------------------------------------------------

class TestCleanCell(unittest.TestCase):
    def test_control_chars_removed(self):
        self.assertEqual(tk._clean_cell("a\x00b\x07c", trim=False), "abc")
        self.assertEqual(tk._clean_cell("\x0b\x0c\x1f\x7f", trim=False), "")

    def test_trim(self):
        self.assertEqual(tk._clean_cell("  a  ", trim=True), "a")
        self.assertEqual(tk._clean_cell("  a  ", trim=False), "  a  ")
        self.assertEqual(tk._clean_cell("\t a \n", trim=True), "a")

    def test_none_and_numbers(self):
        self.assertEqual(tk._clean_cell(None, trim=True), "")
        self.assertEqual(tk._clean_cell(123, trim=True), "123")
        self.assertEqual(tk._clean_cell(1.5, trim=True), "1.5")

    def test_newline_inside_value_kept(self):
        self.assertEqual(tk._clean_cell("a\nb", trim=False), "a\nb")


class TestDateNormalization(unittest.TestCase):
    def test_dash_slash_dot_formats(self):
        headers = ["d"]
        rows = [{"d": "2024-1-2"}, {"d": "2024/12/31"}, {"d": "2024.3.9"}]
        out, rep = tk.clean_rows(headers, rows, date_cols=["d"])
        self.assertEqual([r["d"] for r in out], ["2024-01-02", "2024-12-31", "2024-03-09"])
        self.assertEqual(rep["dates_normalized"], 3)

    def test_already_normalized_counts(self):
        out, rep = tk.clean_rows(["d"], [{"d": "2024-01-02"}], date_cols=["d"])
        self.assertEqual(out[0]["d"], "2024-01-02")
        self.assertEqual(rep["dates_normalized"], 1)

    def test_invalid_dates_untouched(self):
        rows = [{"d": "2024年1月2日"}, {"d": "abc"}, {"d": "2024-1"}, {"d": "2024-1-2-3"}]
        out, rep = tk.clean_rows(["d"], rows, date_cols=["d"])
        self.assertEqual([r["d"] for r in out], ["2024年1月2日", "abc", "2024-1", "2024-1-2-3"])
        self.assertEqual(rep["dates_normalized"], 0)

    def test_autodetect_column_names(self):
        for name in ("日期", "入职日期", "date", "Birth Date", "时间", "time", "日", "day"):
            _, rep = tk.clean_rows([name], [{name: "2024/5/6"}])
            self.assertEqual(rep["dates_normalized"], 1, name)

    def test_non_date_column_not_touched(self):
        out, rep = tk.clean_rows(["值"], [{"值": "2024/5/6"}])
        self.assertEqual(out[0]["值"], "2024/5/6")
        self.assertEqual(rep["dates_normalized"], 0)

    def test_explicit_date_col_overrides_autodetect(self):
        headers = ["日期", "值"]
        rows = [{"日期": "2024/1/1", "值": "2024/1/1"}]
        out, rep = tk.clean_rows(headers, rows, date_cols=["值"])
        self.assertEqual(out[0], {"日期": "2024/1/1", "值": "2024-01-01"})
        self.assertEqual(rep["dates_normalized"], 1)

    def test_single_digit_month_day_padding(self):
        out, _ = tk.clean_rows(["d"], [{"d": "2024/9/9"}], date_cols=["d"])
        self.assertEqual(out[0]["d"], "2024-09-09")

    def test_whitespace_trimmed_before_date_parse(self):
        out, rep = tk.clean_rows(["d"], [{"d": "  2024/1/2  "}], date_cols=["d"])
        self.assertEqual(out[0]["d"], "2024-01-02")
        self.assertEqual(rep["dates_normalized"], 1)


class TestCleanRows(unittest.TestCase):
    def test_trim_drop_empty_and_report(self):
        headers = ["姓名", "birth date", "备注"]
        rows = [
            {"姓名": " 张三 ", "birth date": "2024/3/5", "备注": "\x00"},
            {"姓名": "", "birth date": "", "备注": "  "},
        ]
        out, rep = tk.clean_rows(headers, rows)
        self.assertEqual(out, [{"姓名": "张三", "birth date": "2024-03-05", "备注": ""}])
        self.assertEqual(rep, {
            "input_rows": 2,
            "output_rows": 1,
            "dropped_empty_rows": 1,
            "dates_normalized": 1,
        })

    def test_no_trim(self):
        out, _ = tk.clean_rows(["a"], [{"a": " x "}], trim=False)
        self.assertEqual(out[0]["a"], " x ")

    def test_keep_empty_rows(self):
        out, rep = tk.clean_rows(["a"], [{"a": ""}, {"a": "1"}], drop_empty=False)
        self.assertEqual(len(out), 2)
        self.assertEqual(rep["dropped_empty_rows"], 0)

    def test_missing_cells_filled(self):
        out, _ = tk.clean_rows(["a", "b"], [{"a": "1"}], drop_empty=False)
        self.assertEqual(out, [{"a": "1", "b": ""}])

    def test_all_empty_rows_dropped(self):
        out, rep = tk.clean_rows(["a", "b"], [{"a": "", "b": ""}] * 3)
        self.assertEqual(out, [])
        self.assertEqual(rep["dropped_empty_rows"], 3)
        self.assertEqual(rep["output_rows"], 0)

    def test_control_char_only_row_is_dropped(self):
        out, rep = tk.clean_rows(["a"], [{"a": "\x01\x02"}])
        self.assertEqual(out, [])
        self.assertEqual(rep["dropped_empty_rows"], 1)

    def test_input_rows_not_modified(self):
        headers = ["a"]
        rows = [{"a": " x "}]
        tk.clean_rows(headers, rows)
        self.assertEqual(rows[0]["a"], " x ")

    def test_empty_input(self):
        self.assertEqual(tk.clean_rows(["a"], []), ([], {
            "input_rows": 0, "output_rows": 0,
            "dropped_empty_rows": 0, "dates_normalized": 0,
        }))


# --------------------------------------------------------------------------
# 5. 去重
# --------------------------------------------------------------------------

class TestDeduplicateRows(unittest.TestCase):
    def test_all_columns_casefold_trim(self):
        headers = ["name", "n"]
        rows = [
            {"name": "Ann", "n": "1"},
            {"name": "ann ", "n": "1"},
            {"name": "Bob", "n": "1"},
        ]
        out, removed = tk.deduplicate_rows(headers, rows)
        self.assertEqual(removed, 1)
        self.assertEqual([r["name"] for r in out], ["Ann", "Bob"])
        self.assertIs(out[0], rows[0])

    def test_keeps_first_occurrence(self):
        rows = [{"a": "1", "b": "first"}, {"a": "1", "b": "second"}]
        out, _ = tk.deduplicate_rows(["a", "b"], rows, key_cols=["a"])
        self.assertEqual(out[0]["b"], "first")

    def test_key_cols(self):
        headers = ["a", "b"]
        rows = [{"a": "1", "b": "x"}, {"a": "1", "b": "y"}, {"a": "2", "b": "x"}]
        out, removed = tk.deduplicate_rows(headers, rows, key_cols=["a"])
        self.assertEqual(removed, 1)
        self.assertEqual([(r["a"], r["b"]) for r in out], [("1", "x"), ("2", "x")])

    def test_multiple_key_cols(self):
        headers = ["a", "b"]
        rows = [
            {"a": "1", "b": "x"},
            {"a": "1", "b": "y"},
            {"a": "2", "b": "x"},
        ]
        _, removed = tk.deduplicate_rows(headers, rows, key_cols=["a", "b"])
        self.assertEqual(removed, 0)

    def test_invalid_keys_fallback_to_all(self):
        headers = ["a"]
        rows = [{"a": "1"}, {"a": "1"}]
        out, removed = tk.deduplicate_rows(headers, rows, key_cols=["nope"])
        self.assertEqual(removed, 1)

    def test_empty(self):
        self.assertEqual(tk.deduplicate_rows(["a"], []), ([], 0))

    def test_no_duplicates(self):
        rows = [{"a": "1"}, {"a": "2"}, {"a": "3"}]
        out, removed = tk.deduplicate_rows(["a"], rows)
        self.assertEqual(removed, 0)
        self.assertEqual(len(out), 3)

    def test_blank_values_deduplicated(self):
        rows = [{"a": ""}, {"a": "  "}, {"a": "1"}]
        _, removed = tk.deduplicate_rows(["a"], rows)
        self.assertEqual(removed, 1)

    def test_case_insensitive_unicode(self):
        rows = [{"a": "ÜBER"}, {"a": "über"}]
        _, removed = tk.deduplicate_rows(["a"], rows)
        self.assertEqual(removed, 1)


# --------------------------------------------------------------------------
# 6. 脱敏（4 种）
# --------------------------------------------------------------------------

class TestMaskPhone(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(tk._mask_phone("13812345678"), "138****5678")

    def test_with_separators(self):
        self.assertEqual(tk._mask_phone("138-1234-5678"), "138****5678")

    def test_eleven_digits_exact_middle(self):
        self.assertEqual(tk._mask_phone("13812345678"), "138" + "*" * 4 + "5678")

    def test_seven_digits(self):
        self.assertEqual(tk._mask_phone("1234567"), "123" + "" + "4567")

    def test_nine_digits(self):
        self.assertEqual(tk._mask_phone("123456789"), "123**6789")

    def test_short_value_unchanged(self):
        self.assertEqual(tk._mask_phone("123"), "123")
        self.assertEqual(tk._mask_phone(""), "")

    def test_plus_country_code(self):
        # 非数字字符被剥离：11 位数字 -> 138****5678
        self.assertEqual(tk._mask_phone("+86 138 1234 5678"), "861******5678")
        self.assertEqual(tk._mask_phone("+8613812345678"), "861******5678")


class TestMaskIdCard(unittest.TestCase):
    def test_18_digits(self):
        self.assertEqual(tk._mask_idcard("110101199003077777"), "110101********7777")

    def test_18_with_x(self):
        self.assertEqual(tk._mask_idcard("11010119900307777X"), "110101********777X")

    def test_ten_digits(self):
        self.assertEqual(tk._mask_idcard("1234567890"), "123456" + "" + "7890")

    def test_mid_length(self):
        self.assertEqual(tk._mask_idcard("123456"), "1****6")

    def test_four_digits(self):
        self.assertEqual(tk._mask_idcard("1234"), "1**4")

    def test_too_short(self):
        self.assertEqual(tk._mask_idcard("123"), "123")
        self.assertEqual(tk._mask_idcard(""), "")


class TestMaskEmail(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(tk._mask_email("alice@example.com"), "a***e@example.com")

    def test_two_char_local(self):
        self.assertEqual(tk._mask_email("ab@x.com"), "a*@x.com")

    def test_one_char_local(self):
        self.assertEqual(tk._mask_email("a@x.com"), "a*@x.com")

    def test_not_an_email(self):
        self.assertEqual(tk._mask_email("not-an-email"), "not-an-email")

    def test_domain_kept(self):
        self.assertTrue(tk._mask_email("liming@qq.com").endswith("@qq.com"))


class TestMaskName(unittest.TestCase):
    def test_chinese_three(self):
        self.assertEqual(tk._mask_name("张三丰"), "张**")

    def test_single_char(self):
        self.assertEqual(tk._mask_name("张"), "张")

    def test_empty(self):
        self.assertEqual(tk._mask_name(""), "")

    def test_ascii(self):
        self.assertEqual(tk._mask_name("Bob"), "B**")

    def test_two_chars(self):
        self.assertEqual(tk._mask_name("张三"), "张*")


class TestMaskSensitive(unittest.TestCase):
    def test_four_rules_applied(self):
        headers = ["姓名", "手机号", "身份证号", "邮箱"]
        rows = [{
            "姓名": "李四",
            "手机号": "13800001111",
            "身份证号": "110101199001011234",
            "邮箱": "liming@qq.com",
        }]
        rules = {"姓名": "name", "手机号": "phone", "身份证号": "idcard", "邮箱": "email"}
        out = tk.mask_sensitive(headers, rows, rules)
        self.assertEqual(out[0], {
            "姓名": "李*",
            "手机号": "138****1111",
            "身份证号": "110101********1234",
            "邮箱": "l****g@qq.com",
        })
        # 原数据不被修改
        self.assertEqual(rows[0]["姓名"], "李四")

    def test_unknown_column_ignored(self):
        out = tk.mask_sensitive(["a"], [{"a": "1", "b": "x"}], {"b": "name"})
        self.assertEqual(out, [{"a": "1", "b": "x"}])

    def test_unknown_rule_ignored(self):
        out = tk.mask_sensitive(["a"], [{"a": "张三"}], {"a": "unknown"})
        self.assertEqual(out[0]["a"], "张三")

    def test_empty_value_untouched(self):
        out = tk.mask_sensitive(["email"], [{"email": ""}], {"email": "email"})
        self.assertEqual(out[0]["email"], "")

    def test_empty_rules_copy(self):
        rows = [{"a": "1"}]
        out = tk.mask_sensitive(["a"], rows, {})
        self.assertEqual(out, rows)
        self.assertIsNot(out[0], rows[0])

    def test_header_order_preserved_and_extra_keys_kept(self):
        out = tk.mask_sensitive(["a"], [{"a": "13800001111", "b": "kept"}], {"a": "phone"})
        self.assertEqual(out[0], {"a": "138****1111", "b": "kept"})
        self.assertEqual(list(out[0]), ["a", "b"])

    def test_maskers_registry(self):
        self.assertEqual(set(tk.MASKERS), {"phone", "idcard", "email", "name"})


# --------------------------------------------------------------------------
# 7. 按列拆分
# --------------------------------------------------------------------------

class TestSplitByColumn(unittest.TestCase):
    def test_groups_and_blank_key(self):
        headers = ["city", "n"]
        rows = [
            {"city": "北京", "n": "1"},
            {"city": "上海", "n": "2"},
            {"city": "北京", "n": "3"},
            {"city": "", "n": "4"},
        ]
        groups = tk.split_by_column(headers, rows, "city")
        self.assertEqual(list(groups), ["北京", "上海", "空值"])
        self.assertEqual([r["n"] for r in groups["北京"]], ["1", "3"])
        self.assertEqual([r["n"] for r in groups["上海"]], ["2"])
        self.assertEqual([r["n"] for r in groups["空值"]], ["4"])

    def test_insertion_order(self):
        groups = tk.split_by_column(["k"], [{"k": "b"}, {"k": "a"}, {"k": "b"}], "k")
        self.assertEqual(list(groups), ["b", "a"])

    def test_safe_name_sanitized(self):
        groups = tk.split_by_column(["k"], [
            {"k": "a/b"}, {"k": "a:b"}, {"k": ".hidden "}, {"k": 'x?*"<>|'},
        ], "k")
        self.assertIn("a_b", groups)
        self.assertEqual(len(groups["a_b"]), 2)
        self.assertIn("hidden", groups)
        self.assertIn("x_", groups)

    def test_only_illegal_chars(self):
        groups = tk.split_by_column(["k"], [{"k": "?*"}], "k")
        self.assertEqual(list(groups), ["_"])

    def test_whitespace_value_maps_to_empty(self):
        groups = tk.split_by_column(["k"], [{"k": "   "}], "k")
        self.assertEqual(list(groups), ["empty"])

    def test_long_name_truncated(self):
        groups = tk.split_by_column(["k"], [{"k": "x" * 200}], "k")
        self.assertEqual(len(list(groups)[0]), 80)

    def test_missing_column_raises(self):
        with self.assertRaises(KeyError):
            tk.split_by_column(["a"], [{"a": "1"}], "b")

    def test_empty_rows(self):
        self.assertEqual(tk.split_by_column(["a"], [], "a"), {})

    def test_rows_are_same_objects(self):
        rows = [{"k": "a", "v": "1"}]
        groups = tk.split_by_column(["k", "v"], rows, "k")
        self.assertIs(groups["a"][0], rows[0])

    def test_safe_name_helper(self):
        self.assertEqual(tk._safe_name(""), "empty")
        self.assertEqual(tk._safe_name("   "), "empty")
        self.assertEqual(tk._safe_name("a\x01b"), "a_b")
        self.assertEqual(tk._safe_name("ok"), "ok")


# --------------------------------------------------------------------------
# 8. 处理管线 / ZIP 生成
# --------------------------------------------------------------------------

def _read_zip(blob: bytes) -> tuple[zipfile.ZipFile, dict, dict]:
    zf = zipfile.ZipFile(io.BytesIO(blob))
    report = json.loads(zf.read("report.json").decode("utf-8"))
    contents = {n: zf.read(n) for n in zf.namelist()}
    return zf, report, contents


class TestProcessPayload(unittest.TestCase):
    def test_clean_dedupe_mask_zip(self):
        data = _csv_bytes("姓名,手机号,日期\n张 三,13811112222,2024/1/2\n张 三,13811112222,2024/1/2\n")
        payload = {
            "files": [{"name": "a.csv", "data": _b64(data)}],
            "clean": True,
            "dedupe": True,
            "mask": True,
            "maskRules": {"姓名": "name", "手机号": "phone"},
            "split": False,
        }
        blob, report = tk._process_payload(payload)
        self.assertEqual(report["files"], ["a.csv"])
        self.assertEqual(report["clean"]["dates_normalized"], 2)
        self.assertEqual(report["dedupe"], {"removed": 1, "remaining": 1})
        self.assertEqual(report["mask"], {"rules": {"姓名": "name", "手机号": "phone"}})
        self.assertEqual(report["final_rows"], 1)
        zf, saved, contents = _read_zip(blob)
        self.assertIsNone(zf.testzip())
        zf.close()
        self.assertEqual(set(contents), {"result.csv", "report.json"})
        self.assertEqual(saved, report)
        text = contents["result.csv"].decode("utf-8-sig")
        self.assertIn("张**,138****2222,2024-01-02", text)

    def test_split_zip_single_file_no_source(self):
        payload = {
            "files": [{"name": "s.csv", "data": _b64(_csv_bytes("k,v\na,1\nb,2\na,3\n"))}],
            "split": True,
            "splitCol": "k",
        }
        blob, report = tk._process_payload(payload)
        self.assertEqual(report["split"], {"a": 2, "b": 1})
        self.assertNotIn("_source_file", report["merged"]["headers"])
        _, _, contents = _read_zip(blob)
        self.assertEqual(set(contents), {"split/a.csv", "split/b.csv", "report.json"})
        a = contents["split/a.csv"].decode("utf-8-sig")
        self.assertEqual(a, "k,v\r\na,1\r\na,3\r\n")

    def test_split_unknown_column_falls_back(self):
        payload = {"files": [{"name": "s.csv", "data": _b64(b"k\na\n")}],
                   "split": True, "splitCol": "zzz"}
        blob, report = tk._process_payload(payload)
        self.assertIn("split_error", report)
        _, _, contents = _read_zip(blob)
        self.assertIn("result.csv", contents)
        self.assertNotIn("split", report)

    def test_split_flag_without_col(self):
        payload = {"files": [{"name": "s.csv", "data": _b64(b"k\na\n")}], "split": True, "splitCol": ""}
        _, _, contents = _read_zip(tk._process_payload(payload)[0])
        self.assertEqual(set(contents), {"result.csv", "report.json"})

    def test_split_col_provided_but_flag_off(self):
        payload = {"files": [{"name": "s.csv", "data": _b64(b"k\na\n")}], "split": False, "splitCol": "k"}
        _, report, contents = _read_zip(tk._process_payload(payload)[0])
        self.assertNotIn("split", report)
        self.assertIn("result.csv", contents)

    def test_multiple_files_merge_adds_source(self):
        payload = {"files": [
            {"name": "x.csv", "data": _b64(b"a\n1\n")},
            {"name": "y.csv", "data": _b64(b"a\n2\n")},
        ]}
        _, report, contents = _read_zip(tk._process_payload(payload)[0])
        self.assertEqual(report["merged"]["headers"], ["_source_file", "a"])
        self.assertEqual(report["final_rows"], 2)
        text = contents["result.csv"].decode("utf-8-sig")
        self.assertIn("x.csv,1", text)
        self.assertIn("y.csv,2", text)

    def test_single_file_has_no_source_column(self):
        _, report, _ = _read_zip(tk._process_payload(
            {"files": [{"name": "one.csv", "data": _b64(b"a\n1\n")}]})[0])
        self.assertEqual(report["merged"]["headers"], ["a"])

    def test_no_options_is_plain_passthrough(self):
        _, report, contents = _read_zip(tk._process_payload(
            {"files": [{"name": "r.csv", "data": _b64(b"a,b\n1,2\n")}]})[0])
        for key in ("clean", "dedupe", "mask", "split"):
            self.assertNotIn(key, report)
        self.assertEqual(contents["result.csv"].decode("utf-8-sig"), "a,b\r\n1,2\r\n")

    def test_mixed_encodings_in_one_batch(self):
        payload = {"files": [
            {"name": "gbk.csv", "data": _b64("城市\n北京\n".encode("gbk"))},
            {"name": "utf8.csv", "data": _b64("城市\n上海\n".encode("utf-8-sig"))},
        ], "dedupe": True}
        _, report, contents = _read_zip(tk._process_payload(payload)[0])
        self.assertEqual(report["final_rows"], 2)
        text = contents["result.csv"].decode("utf-8-sig")
        self.assertIn("北京", text)
        self.assertIn("上海", text)

    def test_empty_file_list(self):
        blob, report = tk._process_payload({"files": []})
        self.assertEqual(report["files"], [])
        self.assertEqual(report["final_rows"], 0)
        _, _, contents = _read_zip(blob)
        self.assertEqual(contents["result.csv"], b"\xef\xbb\xbf\r\n")

    def test_missing_data_key_and_bad_base64(self):
        # base64 为空字符串时应视为空文件，而不是崩溃
        _, report, _ = _read_zip(tk._process_payload({"files": [{"name": "e.csv"}]})[0])
        self.assertEqual(report["final_rows"], 0)

    def test_default_filename(self):
        tables = tk._decode_files([{"data": _b64(b"a\n1\n")}])
        self.assertEqual(tables[0][0], "a.csv")

    def test_report_is_utf8_json(self):
        blob, _ = tk._process_payload({"files": [{"name": "c.csv",
                                                  "data": _b64("城市\n北京\n".encode("utf-8"))}]})
        raw = zipfile.ZipFile(io.BytesIO(blob)).read("report.json")
        self.assertIn("c.csv", raw.decode("utf-8"))


# --------------------------------------------------------------------------
# 9. CLI 子命令
# --------------------------------------------------------------------------

class TestCLI(TempDirMixin, unittest.TestCase):
    def _run(self, argv):
        """运行 CLI，同时捕获 stdout/stderr（argparse 直接写 sys.*）。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tk.main(argv)
        return rc, out.getvalue()

    def _write(self, name, data: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(data)
        return p

    # ---- merge ----
    def test_merge(self):
        f1 = self._write("a.csv", b"id,x\n1,2\n")
        f2 = self._write("b.csv", b"id,y\n3,4\n")
        out = self.tmp / "m.csv"
        rc, text = self._run(["merge", str(f1), str(f2), "-o", str(out)])
        self.assertEqual(rc, 0)
        self.assertIn("已合并 2 个文件，2 行", text)
        headers, rows = tk.read_table_bytes(out.read_bytes(), "m.csv")
        self.assertEqual(headers, ["_source_file", "id", "x", "y"])
        self.assertEqual(len(rows), 2)
        self.assertTrue(out.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_merge_no_source(self):
        f1 = self._write("a.csv", b"id\n1\n")
        out = self.tmp / "m.csv"
        self._run(["merge", str(f1), "--no-source", "-o", str(out)])
        self.assertEqual(tk.read_table_bytes(out.read_bytes(), "m.csv")[0], ["id"])

    def test_merge_default_output_name(self):
        f1 = self._write("a.csv", b"id\n1\n")
        cwd = Path.cwd()
        import os
        os.chdir(self.tmp)
        try:
            self._run(["merge", str(f1)])
            headers = tk.read_table_bytes((self.tmp / "merged.csv").read_bytes(), "m.csv")[0]
            # 即使只有一个文件，merge 子命令默认也会加 _source_file 列
            self.assertEqual(headers, ["_source_file", "id"])
        finally:
            os.chdir(cwd)

    # ---- clean ----
    def test_clean(self):
        f = self._write("c.csv", "姓名,日期\n 张三 ,2024/3/4\n".encode("gbk"))
        out = self.tmp / "o.csv"
        rc, text = self._run(["clean", str(f), "-o", str(out), "--date-cols", "日期"])
        self.assertEqual(rc, 0)
        rep = json.loads(text.strip().splitlines()[-1])
        self.assertEqual(rep, {
            "input_rows": 1, "output_rows": 1,
            "dropped_empty_rows": 0, "dates_normalized": 1,
        })
        _, rows = tk.read_table_bytes(out.read_bytes(), "o.csv")
        self.assertEqual(rows[0], {"姓名": "张三", "日期": "2024-03-04"})

    def test_clean_keep_empty_no_trim(self):
        f = self._write("c.csv", b"a,b\n x ,\n")
        out = self.tmp / "o.csv"
        rc, text = self._run(["clean", str(f), "-o", str(out), "--keep-empty", "--no-trim"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(text.strip().splitlines()[-1])), 4)
        _, rows = tk.read_table_bytes(out.read_bytes(), "o.csv")
        self.assertEqual(rows[0]["a"], " x ")

    def test_clean_multiple_date_cols(self):
        f = self._write("c.csv", b"a,b\n2024/1/1,2024/2/2\n")
        out = self.tmp / "o.csv"
        self._run(["clean", str(f), "-o", str(out), "--date-cols", "a, b"])
        _, rows = tk.read_table_bytes(out.read_bytes(), "o.csv")
        self.assertEqual(rows[0], {"a": "2024-01-01", "b": "2024-02-02"})

    def test_clean_autodetect_date_column(self):
        f = self._write("c.csv", "日期\n2024/7/8\n".encode("utf-8"))
        out = self.tmp / "o.csv"
        self._run(["clean", str(f), "-o", str(out)])
        _, rows = tk.read_table_bytes(out.read_bytes(), "o.csv")
        self.assertEqual(rows[0]["日期"], "2024-07-08")

    # ---- dedupe ----
    def test_dedupe(self):
        f = self._write("d.csv", b"a,b\n1,x\n1,y\n1,z\n")
        out = self.tmp / "o.csv"
        rc, text = self._run(["dedupe", str(f), "-o", str(out), "-k", "a"])
        self.assertEqual(rc, 0)
        self.assertIn("去重移除 2 行，剩余 1 行", text)
        _, rows = tk.read_table_bytes(out.read_bytes(), "o.csv")
        self.assertEqual(rows, [{"a": "1", "b": "x"}])

    def test_dedupe_all_columns_default(self):
        f = self._write("d.csv", b"a,b\n1,x\n1,x\n2,y\n")
        out = self.tmp / "o.csv"
        _, text = self._run(["dedupe", str(f), "-o", str(out)])
        self.assertIn("去重移除 1 行，剩余 2 行", text)

    # ---- mask ----
    def test_mask_all_four(self):
        f = self._write("m.csv",
                        "姓名,手机号,身份证号,邮箱\n王五,13900001111,110101199001011234,wang@mail.com\n"
                        .encode("utf-8"))
        out = self.tmp / "o.csv"
        rc, text = self._run(["mask", str(f), "-o", str(out),
                              "-r", "姓名:name,手机号:phone,身份证号:idcard,邮箱:email"])
        self.assertEqual(rc, 0)
        self.assertIn("已脱敏 4 列，1 行", text)
        _, rows = tk.read_table_bytes(out.read_bytes(), "o.csv")
        self.assertEqual(rows[0], {
            "姓名": "王*",
            "手机号": "139****1111",
            "身份证号": "110101********1234",
            "邮箱": "w**g@mail.com",
        })

    def test_mask_unknown_type_errors(self):
        f = self._write("m.csv", b"a\n1\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            tk.main(["mask", str(f), "-r", "a:foo"])
        self.assertIn("未知脱敏类型", err.getvalue())

    def test_mask_malformed_rule_segments_ignored(self):
        f = self._write("m.csv", b"a\n13800001111\n")
        out = self.tmp / "o.csv"
        _, text = self._run(["mask", str(f), "-o", str(out), "-r", "garbage,a:phone"])
        self.assertIn("已脱敏 1 列", text)
        _, rows = tk.read_table_bytes(out.read_bytes(), "o.csv")
        self.assertEqual(rows[0]["a"], "138****1111")

    def test_mask_requires_rules(self):
        f = self._write("m.csv", b"a\n1\n")
        with self.assertRaises(SystemExit):
            self._run(["mask", str(f)])

    # ---- split ----
    def test_split(self):
        f = self._write("s.csv", "城市,值\n北京,1\n上海,2\n北京,3\n".encode("utf-8"))
        outdir = self.tmp / "out"
        rc, text = self._run(["split", str(f), "-c", "城市", "-d", str(outdir)])
        self.assertEqual(rc, 0)
        self.assertIn("已拆分为 2 个文件", text)
        self.assertEqual(sorted(p.name for p in outdir.iterdir()), sorted(["上海.csv", "北京.csv"]))
        _, rows = tk.read_table_bytes((outdir / "北京.csv").read_bytes(), "北京.csv")
        self.assertEqual(len(rows), 2)

    def test_split_default_outdir(self):
        import os
        f = self._write("s.csv", b"k\n1\n")
        cwd = Path.cwd()
        os.chdir(self.tmp)
        try:
            self._run(["split", str(f), "-c", "k"])
            self.assertTrue((self.tmp / "split" / "1.csv").exists())
        finally:
            os.chdir(cwd)

    def test_split_missing_column_raises(self):
        f = self._write("s.csv", b"k\n1\n")
        with self.assertRaises(KeyError):
            self._run(["split", str(f), "-c", "nope", "-d", str(self.tmp / "o")])

    # ---- 其他 ----
    def test_no_command_errors(self):
        with self.assertRaises(SystemExit):
            self._run([])

    def test_unknown_command_errors(self):
        with self.assertRaises(SystemExit):
            self._run(["nope"])

    def test_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            tk.main(["--help"])
        self.assertIn("merge", out.getvalue())


# --------------------------------------------------------------------------
# 10. WebUI（真实 HTTP 服务）
# --------------------------------------------------------------------------

class TestServer(unittest.TestCase):
    def setUp(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = tk.create_server("127.0.0.1", self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()

    def _post(self, path, payload, raw: bytes | None = None):
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    # ---- GET ----
    def test_get_index(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        text = body.decode("utf-8")
        self.assertIn("表格工具箱", text)
        self.assertIn("/api/process", text)
        self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_get_index_html_alias(self):
        status, _, body = self._get("/index.html")
        self.assertEqual(status, 200)
        self.assertIn("表格工具箱", body.decode("utf-8"))

    def test_get_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/nope", timeout=10)
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(ctx.exception.read(), b"Not Found")

    def test_get_api_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/api/process", timeout=10)
        self.assertEqual(ctx.exception.code, 404)

    def test_head_index(self):
        req = urllib.request.Request(self.base + "/", method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_head_404(self):
        req = urllib.request.Request(self.base + "/nope", method="HEAD")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 404)

    # ---- POST ----
    def test_post_endtoend_zip(self):
        gbk = ("姓名,手机号,城市,日期\n"
               "赵 六,13800001234,北京,2024-1-9\n"
               "赵 六,13800001234,北京,2024-1-9\n"
               "孙七,13911112222,上海,2024/2/3\n").encode("gbk")
        payload = {
            "files": [{"name": "people.csv", "data": _b64(gbk)}],
            "clean": True, "dedupe": True, "mask": True,
            "maskRules": {"手机号": "phone"},
            "split": True, "splitCol": "城市",
        }
        status, headers, body = self._post("/api/process", payload)
        self.assertEqual(status, 200)
        self.assertIn("application/zip", headers.get("Content-Type", ""))
        self.assertIn("tablekit_output.zip", headers.get("Content-Disposition", ""))
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            self.assertIsNone(zf.testzip())
            names = set(zf.namelist())
            self.assertEqual(names, {"report.json", "split/北京.csv", "split/上海.csv"})
            report = json.loads(zf.read("report.json").decode("utf-8"))
            self.assertEqual(report["merged"]["rows"], 3)
            self.assertEqual(report["dedupe"]["removed"], 1)
            self.assertEqual(report["final_rows"], 2)
            self.assertEqual(report["split"], {"北京": 1, "上海": 1})
            bj = zf.read("split/北京.csv").decode("utf-8-sig")
            sh = zf.read("split/上海.csv").decode("utf-8-sig")
        self.assertIn("赵 六,138****1234,北京,2024-01-09", bj)
        self.assertIn("孙七,139****2222,上海,2024-02-03", sh)

    def test_post_multi_files_merge(self):
        payload = {"files": [
            {"name": "a.csv", "data": _b64(b"id,v\n1,a\n")},
            {"name": "b.csv", "data": _b64(b"id,w\n2,b\n")},
        ], "split": False}
        status, _, body = self._post("/api/process", payload)
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            report = json.loads(zf.read("report.json").decode("utf-8"))
            text = zf.read("result.csv").decode("utf-8-sig")
        self.assertEqual(report["merged"]["headers"], ["_source_file", "id", "v", "w"])
        self.assertIn("a.csv,1,a,", text)
        self.assertIn("b.csv,2,,b", text)

    def test_post_minimal_payload(self):
        status, headers, body = self._post("/api/process", {"files": [
            {"name": "r.csv", "data": _b64(b"a\n1\n")}]})
        self.assertEqual(status, 200)
        self.assertIn("application/zip", headers["Content-Type"])
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            self.assertIn("result.csv", zf.namelist())

    def test_post_404_path(self):
        status, _, body = self._post("/api/other", {"files": []})
        self.assertEqual(status, 404)
        self.assertEqual(body, b"Not Found")

    def test_post_bad_json_400(self):
        status, headers, body = self._post("/api/process", None, raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_post_empty_body_400(self):
        status, _, body = self._post("/api/process", None, raw=b"")
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_post_invalid_base64_is_lenient(self):
        # base64.b64decode 默认宽松解析，非法字符被忽略 -> 视为空文件，仍返回有效 ZIP
        status, headers, body = self._post("/api/process",
                                           {"files": [{"name": "x.csv", "data": "!!!!"}]})
        self.assertEqual(status, 200)
        self.assertIn("application/zip", headers["Content-Type"])
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            report = json.loads(zf.read("report.json").decode("utf-8"))
        self.assertEqual(report["final_rows"], 0)

    def test_server_version(self):
        self.assertIsInstance(self.server, tk.HTTPServer)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
