# 样例数据

计费期间一律 `[期间起, 期间止)` 半开区间，日期按订阅时区的本地日；金额单位元，入库 Decimal。

| 文件 | 编码 | 用途 |
|---|---|---|
| `subscriptions_dirty.csv` | UTF-8 带 BOM | 主样例：千分位、全角数字、三位小数、期间倒置、多一列、重复键冲突、未知时区、未知状态、缺价、空行 |
| `changes_dirty.csv` | UTF-8 | 变更主样例：Z 与 +08:00 混用、夏令时重叠段、同日两次变更、同一时刻两条、乱序迟到、期间外变更、非零点生效、未知订阅 |
| `subscriptions_gbk.csv` | GB18030 | 编码回退链：同一套代码路径读国标编码 |
| `subscriptions_header_only.csv` | UTF-8 | 空态：仅表头 |
| `subscriptions_blank.csv` | UTF-8 | 空态：全空行 |
| `subscriptions_quoted.csv` | UTF-8 | 引号内含换行：逻辑行数 = 记录数 |
| `subscriptions_truncated.csv` | UTF-8 | 截断尾行：引号未闭合 |
| `subscriptions_cancel.csv` + `changes_cancel.csv` | UTF-8 | 结转样例（第一期）：SUB-2001 中途取消，未用额度 2057.14 元开单结转；同账户同期间的 SUB-2002 不被抵扣 |
| `subscriptions_cancel_next.csv` + `changes_cancel_next.csv` | UTF-8 | 结转样例（下一账期）：SUB-2003 应付被余额抵到 0，SUB-2004 抵剩余 237.79 元后应付 362.21 元，余额归零 |

主样例里有 16 条订阅记录（其中 6 条会被隔离）、14 条变更记录（其中 1 条未知订阅被隔离）。

结转样例的用法：先采信 `subscriptions_cancel.csv` + `changes_cancel.csv`（2026-02 期间），
再采信 `subscriptions_cancel_next.csv` + `changes_cancel_next.csv`（2026-03 期间）；
账单页的「期初结转 / 结转抵扣 / 应付 / 期末结转」与「结转余额」区的期末余额逐期对得上。
