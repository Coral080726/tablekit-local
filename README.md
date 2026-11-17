# TableKit Local v1.1（双引擎专业版）· 纯本地表格批量合并/清洗/去重/脱敏/分表工具箱

> **100% 本地离线运行 · 零联网 · 敏感数据绝不出你的电脑**

## 一、最快上手（无需安装任何软件！）

- **零安装桌面网页版（推荐财务/运营/行政/电商卖家）**：
  无需安装 Python 或任何插件！直接用 Chrome / Edge / Safari / Firefox 双击打开本压缩包内的 **`TableKit-免安装桌面网页版.html`**，拖入多个 `.csv` / `.tsv` 表格即可在本地浏览器内存里完成合并、清洗、去重、隐私脱敏与按列拆表打包 ZIP 下载！（不确定怎么用？点击页面右上角【加载示例数据一键体验】3 秒看懂！）

- **Python 高性能版（适合海量数据批处理 / 自动化脚本集成）**：
  - Windows：安装 Python 3.9+ 后双击 `start_windows.bat`
  - Mac / Linux：终端运行 `./start_mac_linux.sh`
  - 若需直接读写原生 `.xlsx`，执行一次 `pip install openpyxl` 即可解锁。

## 二、五大高频痛点一键搞定

1. **多表智能对齐合并**：自动探测 UTF-8 / GBK / GB18030 中文编码，不同部门表格列顺序不一致、缺列多列自动取并集对齐，并标注 `_source_file` 来源文件。
2. **脏数据清洗与日期标准化**：一键剔除首尾空格与隐藏控制符、剔除空行，将 `2026/1/5`、`2026.01.05` 等混乱格式统一转为 `2026-01-05`。
3. **按主键精准去重**：支持全行去重或按`订单号`/`手机号`等多列组合去重，自动统计剔除重复数。
4. **合规隐私脱敏**：内置手机号（`138****5678`）、身份证、邮箱、姓名 4 类一键打码，外发表格安全合规。
5. **按字段自动拆表**：按`城市`/`部门`/`负责人`一键拆分为数十个独立表格（UTF-8 BOM 编码，Excel 双击绝不乱码）并一键打包 ZIP 下载。

## 三、命令行（CLI）速查与自动化验证

```bash
python3 tablekit.py merge sample_data/orders_dept_a.csv sample_data/orders_dept_b.csv -o merged.csv
python3 tablekit.py clean merged.csv -o cleaned.csv --date-cols 下单日期
python3 tablekit.py dedupe cleaned.csv -o deduped.csv --keys 订单号
python3 tablekit.py mask deduped.csv -o masked.csv --rules 手机号:phone,客户姓名:name,邮箱:email
python3 tablekit.py split masked.csv -o split_out/ --col 城市

# 运行内置的 151 项自动化单元测试
python3 -m unittest
```
