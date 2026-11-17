#!/usr/bin/env python3
"""tablekit: 轻量表格工具库 + 命令行 + 本地网页界面。"""

import argparse
import base64
import csv
import io
import json
import os
import re
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    import openpyxl
except Exception:
    openpyxl = None

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DATE_RE = re.compile(r"^(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})$")


def _is_xlsx(filename: str) -> bool:
    return filename.lower().endswith((".xlsx", ".xlsm"))


def read_table_bytes(data: bytes, filename: str = "a.csv") -> tuple[list[str], list[dict[str, str]]]:
    if _is_xlsx(filename):
        if openpyxl is None:
            raise RuntimeError("需要 openpyxl 才能读取 .xlsx")
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        try:
            header_row = next(it)
        except StopIteration:
            wb.close()
            return [], []
        headers = [("" if c is None else str(c)) for c in header_row]
        headers = _dedupe_headers(headers)
        rows = []
        for raw in it:
            cells = ["" if c is None else str(c) for c in raw]
            cells += [""] * (len(headers) - len(cells))
            rows.append(dict(zip(headers, cells[: len(headers)])))
        wb.close()
        return headers, rows

    text = None
    for enc in ENCODINGS:
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = data.decode("utf-8", errors="replace")

    delimiter = "\t" if filename.lower().endswith((".tsv", ".tab")) else ","
    sample = text[:8192]
    if delimiter == ",":
        try:
            if csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except Exception:
            pass
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    records = [r for r in reader]
    if not records:
        return [], []
    headers = _dedupe_headers([str(c).strip() for c in records[0]])
    rows = []
    for rec in records[1:]:
        if not any(c.strip() for c in rec):
            continue
        rec = list(rec) + [""] * (len(headers) - len(rec))
        rows.append(dict(zip(headers, rec[: len(headers)])))
    return headers, rows


def _dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, h in enumerate(headers):
        name = h or f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


def write_csv_bytes(headers: list[str], rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([row.get(h, "") for h in headers])
    return buf.getvalue().encode("utf-8-sig")


def merge_tables(
    tables: list[tuple[str, list[str], list[dict[str, str]]]],
    add_source_col: bool = True,
) -> tuple[list[str], list[dict[str, str]]]:
    union: list[str] = []
    seen: set[str] = set()
    for _, headers, _ in tables:
        for h in headers:
            if h not in seen:
                seen.add(h)
                union.append(h)
    out: list[dict[str, str]] = []
    for name, headers, rows in tables:
        for row in rows:
            item = {h: row.get(h, "") for h in union}
            if add_source_col:
                item = {"_source_file": name, **item}
            out.append(item)
    if add_source_col:
        union = ["_source_file", *union]
    return union, out


def _clean_cell(value: str, trim: bool) -> str:
    if value is None:
        return ""
    text = CONTROL_RE.sub("", str(value))
    if trim:
        text = text.strip()
    return text


def clean_rows(
    headers: list[str],
    rows: list[dict[str, str]],
    trim: bool = True,
    drop_empty: bool = True,
    date_cols: list[str] | None = None,
) -> tuple[list[dict[str, str]], dict]:
    date_set = set(date_cols or [])
    if not date_set:
        date_set = {
            h for h in headers
            if re.search(r"日期|date|时间|time|日|day", h, re.I)
        }
    out: list[dict[str, str]] = []
    dropped = 0
    dates_fixed = 0
    for row in rows:
        item: dict[str, str] = {}
        for h in headers:
            val = _clean_cell(row.get(h, ""), trim)
            if h in date_set:
                m = DATE_RE.match(val)
                if m:
                    y, mo, d = m.groups()
                    val = f"{y}-{int(mo):02d}-{int(d):02d}"
                    dates_fixed += 1
            item[h] = val
        if drop_empty and not any(v for v in item.values()):
            dropped += 1
            continue
        out.append(item)
    report = {
        "input_rows": len(rows),
        "output_rows": len(out),
        "dropped_empty_rows": dropped,
        "dates_normalized": dates_fixed,
    }
    return out, report


def deduplicate_rows(
    headers: list[str],
    rows: list[dict[str, str]],
    key_cols: list[str] | None = None,
) -> tuple[list[dict[str, str]], int]:
    keys = [k for k in (key_cols or headers) if k in headers] or list(headers)
    seen: set[tuple[str, ...]] = set()
    out: list[dict[str, str]] = []
    removed = 0
    for row in rows:
        sig = tuple(str(row.get(k, "")).strip().casefold() for k in keys)
        if sig in seen:
            removed += 1
            continue
        seen.add(sig)
        out.append(row)
    return out, removed


def _mask_phone(v: str) -> str:
    digits = re.sub(r"\D", "", v)
    if len(digits) >= 7:
        return digits[:3] + "*" * (len(digits) - 7) + digits[-4:]
    return v


def _mask_idcard(v: str) -> str:
    if len(v) >= 10:
        return v[:6] + "*" * (len(v) - 10) + v[-4:]
    if len(v) >= 4:
        return v[0] + "*" * (len(v) - 2) + v[-1]
    return v


def _mask_email(v: str) -> str:
    if "@" not in v:
        return v
    local, _, domain = v.partition("@")
    if len(local) <= 2:
        masked = local[:1] + "*"
    else:
        masked = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked}@{domain}"


def _mask_name(v: str) -> str:
    if not v:
        return v
    if len(v) == 1:
        return v
    return v[0] + "*" * (len(v) - 1)


MASKERS = {
    "phone": _mask_phone,
    "idcard": _mask_idcard,
    "email": _mask_email,
    "name": _mask_name,
}


def mask_sensitive(
    headers: list[str],
    rows: list[dict[str, str]],
    rules: dict[str, str],
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        item = dict(row)
        for col, rule in rules.items():
            if col not in headers:
                continue
            fn = MASKERS.get(rule)
            if fn is None:
                continue
            val = item.get(col, "")
            if val:
                item[col] = fn(str(val))
        out.append(item)
    return out


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(name)).strip(" .")
    cleaned = cleaned[:80] or "empty"
    return cleaned


def split_by_column(
    headers: list[str],
    rows: list[dict[str, str]],
    col: str,
) -> dict[str, list[dict[str, str]]]:
    if col not in headers:
        raise KeyError(f"列不存在: {col}")
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        key = _safe_name(row.get(col, "") or "空值")
        groups.setdefault(key, []).append(row)
    return groups


def _decode_files(files: list[dict]) -> list[tuple[str, list[str], list[dict[str, str]]]]:
    tables = []
    for f in files:
        name = f.get("name") or "a.csv"
        data = base64.b64decode(f.get("data", ""))
        headers, rows = read_table_bytes(data, name)
        tables.append((name, headers, rows))
    return tables


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>表格工具箱</title>
<style>
:root{--bg:#f5f7fb;--card:#fff;--pri:#3b6cf6;--txt:#1f2937;--sub:#6b7280;--bd:#e5e7eb}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--txt);padding:32px 16px}
.wrap{max-width:820px;margin:0 auto}
h1{font-size:24px;margin:0 0 6px}
.sub{color:var(--sub);margin-bottom:24px;font-size:14px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:22px;box-shadow:0 2px 10px rgba(15,23,42,.04);margin-bottom:18px}
.drop{border:2px dashed #c7d2fe;border-radius:12px;padding:34px;text-align:center;color:var(--sub);cursor:pointer;transition:.2s;background:#f8faff}
.drop:hover,.drop.on{border-color:var(--pri);background:#eef3ff;color:var(--pri)}
.files{list-style:none;padding:0;margin:14px 0 0;font-size:14px}
.files li{padding:7px 10px;background:#f3f4f6;border-radius:8px;margin-bottom:6px;display:flex;justify-content:space-between}
.files span{color:var(--sub)}
.opts{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:560px){.opts{grid-template-columns:1fr}}
label.opt{display:flex;align-items:center;gap:9px;padding:11px 13px;border:1px solid var(--bd);border-radius:10px;cursor:pointer;font-size:14px;background:#fbfcff}
label.opt:hover{border-color:#c7d2fe}
input[type=checkbox]{width:17px;height:17px;accent-color:var(--pri)}
input[type=text]{width:100%;padding:10px 12px;border:1px solid var(--bd);border-radius:9px;font-size:14px;margin-top:10px}
button{background:var(--pri);color:#fff;border:0;border-radius:10px;padding:13px 26px;font-size:15px;cursor:pointer;width:100%;font-weight:600}
button:disabled{background:#a5b4fc;cursor:not-allowed}
#msg{margin-top:12px;font-size:14px;color:var(--sub);min-height:20px}
.ok{color:#059669!important}
.err{color:#dc2626!important}
</style></head><body><div class="wrap">
<h1>表格工具箱</h1>
<div class="sub">合并 · 清洗 · 去重 · 脱敏 · 拆分，全程在本机处理，数据不会离开浏览器。</div>
<div class="card">
<div class="drop" id="drop">点击或拖拽文件到此处<br><span style="font-size:13px">支持 CSV / TSV / XLSX，可多选</span></div>
<input type="file" id="file" multiple accept=".csv,.tsv,.xlsx,.xlsm" hidden>
<ul class="files" id="list"></ul>
</div>
<div class="card">
<div class="opts">
<label class="opt"><input type="checkbox" id="doClean" checked>清洗空行 / 日期</label>
<label class="opt"><input type="checkbox" id="doDedupe" checked>去重</label>
<label class="opt"><input type="checkbox" id="doMask">脱敏</label>
<label class="opt"><input type="checkbox" id="doSplit">按列拆分</label>
</div>
<input type="text" id="maskCols" placeholder="脱敏列（逗号分隔，如：手机号,身份证,邮箱,姓名）">
<input type="text" id="splitCol" placeholder="拆分列名（留空则不拆分）">
<div style="margin-top:16px"><button id="go" disabled>开始处理并下载 ZIP</button></div>
<div id="msg"></div>
</div>
</div><script>
const $=s=>document.querySelector(s);let picked=[];
const drop=$("#drop"),file=$("#file"),list=$("#list"),go=$("#go"),msg=$("#msg");
drop.onclick=()=>file.click();
drop.ondragover=e=>{e.preventDefault();drop.classList.add("on")};
drop.ondragleave=()=>drop.classList.remove("on");
drop.ondrop=e=>{e.preventDefault();drop.classList.remove("on");add(e.dataTransfer.files)};
file.onchange=e=>add(e.target.files);
function add(fs){for(const f of fs)picked.push(f);render()}
function render(){
 list.innerHTML=picked.map((f,i)=>`<li>${f.name}<span>${(f.size/1024).toFixed(1)} KB <a href="#" data-i="${i}" style="color:#dc2626;margin-left:8px;text-decoration:none">删除</a></span></li>`).join("");
 go.disabled=!picked.length;
 list.querySelectorAll("a").forEach(a=>a.onclick=e=>{e.preventDefault();picked.splice(+a.dataset.i,1);render()});
}
const read=f=>new Promise(r=>{const fr=new FileReader();fr.onload=()=>r({name:f.name,data:fr.result.split(",")[1]||""});fr.readAsDataURL(f)});
go.onclick=async()=>{
 go.disabled=true;msg.className="";msg.textContent="处理中…";
 try{
  const files=await Promise.all(picked.map(read));
  const rules={};
  const colMap={"手机":"phone","手机号":"phone","电话":"phone","phone":"phone","身份证":"idcard","身份证号":"idcard","idcard":"idcard","邮箱":"email","email":"email","姓名":"name","name":"name"};
  $("#maskCols").value.split(/[,，]/).map(s=>s.trim()).filter(Boolean).forEach(c=>{
    const k=c.toLowerCase();rules[c]=colMap[k]||colMap[c]||"name";
  });
  const payload={files,clean:$("#doClean").checked,dedupe:$("#doDedupe").checked,
    mask:$("#doMask").checked,maskRules:rules,split:$("#doSplit").checked,splitCol:$("#splitCol").value.trim()};
  const res=await fetch("/api/process",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
  if(!res.ok)throw new Error("服务器返回 "+res.status);
  const blob=await res.blob();
  const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="tablekit_output.zip";a.click();
  URL.revokeObjectURL(a.href);
  msg.className="ok";msg.textContent="完成，已开始下载 tablekit_output.zip";
 }catch(e){msg.className="err";msg.textContent="失败："+e.message}
 finally{go.disabled=!picked.length}
};
</script></body></html>
"""


def _process_payload(payload: dict) -> tuple[bytes, dict]:
    tables = _decode_files(payload.get("files") or [])
    headers, rows = merge_tables(tables, add_source_col=len(tables) > 1)
    report: dict = {"files": [t[0] for t in tables], "merged": {"headers": headers, "rows": len(rows)}}
    if payload.get("clean"):
        rows, rep = clean_rows(headers, rows)
        report["clean"] = rep
    if payload.get("dedupe"):
        rows, removed = deduplicate_rows(headers, rows)
        report["dedupe"] = {"removed": removed, "remaining": len(rows)}
    if payload.get("mask") and payload.get("maskRules"):
        rows = mask_sensitive(headers, rows, payload["maskRules"])
        report["mask"] = {"rules": payload["maskRules"]}
    report["final_rows"] = len(rows)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        split_col = payload.get("splitCol") if payload.get("split") else None
        if split_col:
            try:
                groups = split_by_column(headers, rows, split_col)
            except KeyError as exc:
                report["split_error"] = str(exc)
                groups = {}
            if groups:
                for key, grp in groups.items():
                    zf.writestr(f"split/{key}.csv", write_csv_bytes(headers, grp))
                report["split"] = {k: len(v) for k, v in groups.items()}
            else:
                zf.writestr("result.csv", write_csv_bytes(headers, rows))
        else:
            zf.writestr("result.csv", write_csv_bytes(headers, rows))
        zf.writestr("report.json", json.dumps(report, ensure_ascii=False, indent=2))
    return buf.getvalue(), report


def create_server(host: str = "127.0.0.1", port: int = 8765) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "tablekit/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send(404, b"Not Found", "text/plain; charset=utf-8")

        do_HEAD = do_GET

        def do_POST(self):
            if self.path != "/api/process":
                self._send(404, b"Not Found", "text/plain; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 200 * 1024 * 1024:
                    raise ValueError("请求体大小非法")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                data, _ = _process_payload(payload)
                self._send(
                    200, data, "application/zip",
                    {"Content-Disposition": 'attachment; filename="tablekit_output.zip"'},
                )
            except Exception as exc:
                body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self._send(400, body, "application/json; charset=utf-8")

    return HTTPServer((host, port), Handler)


def _load_files(paths: list[str]) -> list[tuple[str, list[str], list[dict[str, str]]]]:
    return [
        (Path(p).name, *read_table_bytes(Path(p).read_bytes(), Path(p).name))
        for p in paths
    ]


def _write(path: str, headers: list[str], rows: list[dict[str, str]]) -> None:
    Path(path).write_bytes(write_csv_bytes(headers, rows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tablekit", description="表格合并 / 清洗 / 去重 / 脱敏 / 拆分工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_merge = sub.add_parser("merge", help="合并多个表格")
    p_merge.add_argument("inputs", nargs="+")
    p_merge.add_argument("-o", "--output", default="merged.csv")
    p_merge.add_argument("--no-source", action="store_true", help="不添加 _source_file 列")

    p_clean = sub.add_parser("clean", help="清洗表格")
    p_clean.add_argument("input")
    p_clean.add_argument("-o", "--output", default="cleaned.csv")
    p_clean.add_argument("--no-trim", action="store_true")
    p_clean.add_argument("--keep-empty", action="store_true")
    p_clean.add_argument("--date-cols", default="", help="逗号分隔的日期列")

    p_dedupe = sub.add_parser("dedupe", help="去重")
    p_dedupe.add_argument("input")
    p_dedupe.add_argument("-o", "--output", default="deduped.csv")
    p_dedupe.add_argument("-k", "--keys", default="", help="逗号分隔的关键列，默认全部列")

    p_mask = sub.add_parser("mask", help="脱敏")
    p_mask.add_argument("input")
    p_mask.add_argument("-o", "--output", default="masked.csv")
    p_mask.add_argument("-r", "--rules", required=True, help="列:类型,... 类型=phone/idcard/email/name")

    p_split = sub.add_parser("split", help="按列拆分")
    p_split.add_argument("input")
    p_split.add_argument("-c", "--col", required=True)
    p_split.add_argument("-d", "--outdir", default="split")

    p_web = sub.add_parser("webui", help="启动本地网页界面")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)

    if args.cmd == "merge":
        headers, rows = merge_tables(_load_files(args.inputs), add_source_col=not args.no_source)
        _write(args.output, headers, rows)
        print(f"已合并 {len(args.inputs)} 个文件，{len(rows)} 行 -> {args.output}")
    elif args.cmd == "clean":
        headers, rows = read_table_bytes(Path(args.input).read_bytes(), Path(args.input).name)
        date_cols = [c.strip() for c in args.date_cols.split(",") if c.strip()]
        rows, report = clean_rows(headers, rows, trim=not args.no_trim,
                                  drop_empty=not args.keep_empty, date_cols=date_cols or None)
        _write(args.output, headers, rows)
        print(json.dumps(report, ensure_ascii=False))
    elif args.cmd == "dedupe":
        headers, rows = read_table_bytes(Path(args.input).read_bytes(), Path(args.input).name)
        keys = [c.strip() for c in args.keys.split(",") if c.strip()]
        rows, removed = deduplicate_rows(headers, rows, keys or None)
        _write(args.output, headers, rows)
        print(f"去重移除 {removed} 行，剩余 {len(rows)} 行 -> {args.output}")
    elif args.cmd == "mask":
        headers, rows = read_table_bytes(Path(args.input).read_bytes(), Path(args.input).name)
        rules = {}
        for part in args.rules.split(","):
            if ":" in part:
                col, rule = part.split(":", 1)
                rules[col.strip()] = rule.strip()
        unknown = [r for r in rules.values() if r not in MASKERS]
        if unknown:
            parser.error(f"未知脱敏类型: {', '.join(unknown)}")
        rows = mask_sensitive(headers, rows, rules)
        _write(args.output, headers, rows)
        print(f"已脱敏 {len(rules)} 列，{len(rows)} 行 -> {args.output}")
    elif args.cmd == "split":
        headers, rows = read_table_bytes(Path(args.input).read_bytes(), Path(args.input).name)
        groups = split_by_column(headers, rows, args.col)
        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        for key, grp in groups.items():
            _write(str(outdir / f"{key}.csv"), headers, grp)
        print(f"已拆分为 {len(groups)} 个文件 -> {outdir}")
    elif args.cmd == "webui":
        server = create_server(args.host, args.port)
        print(f"服务已启动: http://{args.host}:{args.port}  (Ctrl+C 退出)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
