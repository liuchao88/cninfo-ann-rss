# cninfo-ann-rss · 巨潮公告关键词监控

全市场公告 → 标题事件词粗筛 → 下载 PDF 抽正文 → 用 AI 产业链词库匹配 → 生成 RSS。

这是用来替掉死掉的第三方源 `tmwgsicp/ForgeRSS`（内容停在 2026-09-21，Action 连续失败）的自建替代。
产物：`feed/rss.xml`，发布到 GitHub Pages，供 FreshRSS 订阅。

## 筛选规则（2026-09-26 与用户确认）

1. **抓取**：巨潮官方接口 `hisAnnouncement/query`，深市 `plate=sz` + 沪市 `plate=sh`，
   每次翻页 30 条（服务端上限），取最近 3 天窗口并按 `announcementId` 去重
   （巨潮按"天"查不稳——实测深市 9-25 只返回 87 条、9-24 是 1089 条，必须用重叠窗口兜底）。
2. **标题粗筛**：标题里出现通用事件词（合同/中标/订单/投资/收购/重组/扩产/产能/量产/投产/定点…）才进入下一步。
   标题以「3-2-1」这种编号开头的附件分片直接跳过。
3. **正文匹配**：下载 PDF（`static.cninfo.com.cn`）抽文本，先剔掉免责样板句（"不构成重大资产重组""不涉及关联交易"等），再用词库匹配：
   - **强档**（signal_weights 的 critical + high，且不含被降级的模板词）：**单命中即通过**
   - **弱档**（其余所有词）：同一篇里命中 **≥2 个不同词**才通过
   - **降级词** `重组 / 并购 / 采购`：这三个词在公告里是模板语言（"不构成重大资产重组""本次交易构成关联交易""债务重组"），
     单独命中不算；但它们和别的词一样计入弱档的"≥2 个不同词" —— 也就是说"并购+重组"这种纯重组公告会进来
     （用户 2026-09-26 明确要保留：重组本身也是投资机会）
4. **同事件去重**：同公司、同一天、标题相似度 ≥0.55 的只留一条。

## 产量（2026-09-26 实测，3 天窗口）

| 环节 | 数量 |
|---|---|
| 全市场公告（去重后） | 2505 条 / 3 天 |
| 标题含事件词 | 348 条 |
| 下载 PDF | 346 份（失败 2） |
| 通过筛选 | 71 条（通过率 21%，约 **24 条/天**）|

## 文件

```
AI_KEYWORDS.json                    （已删除）词库搬到独立仓库 →
scripts/fetch_ann.py               抓取 + 筛选 + 生成 RSS
.github/workflows/ann-watch.yml    每小时跑一次（GitHub cron 有稀释，实际 1~3 小时一次）
feed/rss.xml                       产物（由 Action 自动提交）
state.json                         已处理公告 id + 累积命中（由 Action 自动提交）
```

## 依赖

`pypdf`（workflow 里 `pip install pypdf`）。其余全是 Python 标准库。

## 本地怎么跑

```bash
pip install pypdf
python scripts/fetch_ann.py --days 3        # 正常跑
python scripts/fetch_ann.py --dry           # 只统计不落盘
python scripts/fetch_ann.py --limit 20      # 只处理前 20 份（调试用）
python scripts/fetch_ann.py --feed-only     # 只按 state.json 重新生成 feed
```

## 常用参数（都在 fetch_ann.py 顶部）

| 常量 | 作用 |
|---|---|
| `EVENT_WORDS` | 标题粗筛词。放宽=抓更多 PDF（更全、更慢），收紧=更快但可能漏 |
| `DEMOTE_STRONG` | 从强档降级的模板词（默认 重组/并购/采购） |
| `MAX_PDF` / `MAX_PAGES` / `MAX_MB` | 单轮 PDF 数量、单份抽页数、单份体积上限 |
| `WINDOW_DAYS` | 每次抓最近几天（默认 3，别调成 1） |
| `BOILER_PATTERNS` | 剔除的免责样板句正则 |

## 与 hudong-rss 的关系

词库唯一真源：`liuchao88/a-share-keywords` 仓库（每周一自动补词，按行业分文件、每个文件带 enabled 开关）。
本仓库运行时读 `keywords/index.json` → 逐个取 `enabled: true` 的行业文件 → 合并。
取不到就**这一轮不抓**（先前的"本地副本兜底"已删除：用过期词库筛是隐性漏，比不抓更糟）。
**改词只改 a-share-keywords 那一份**，不要在本仓库留副本。
