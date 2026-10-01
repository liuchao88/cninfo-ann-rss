#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巨潮公告 · 关键词监控（正文级）—— 抓全市场公告 → 标题粗筛 → 下 PDF 抽正文 → 词库匹配

筛选规则（用户 2026-09-26 定）：
  1. 标题粗筛：标题里出现通用事件词（合同/中标/订单/投资/收购/重组/扩产…）才进入下一步
  2. 正文匹配：下载 PDF 抽文本，用 AI_KEYWORDS.json 的词匹配
     critical/high 档的词：单命中即算通过
     medium/low 档 + 其余主题词：同一条公告正文里命中 ≥2 个不同词才算通过
  3. 同一公告的"附件分片"（标题以 3-2-1 这种编号开头）跳过，避免一单多报

输出：feed/rss.xml（RSS 2.0）+ state.json（已见公告 id）+ 控制台统计
用法：python3 fetch_ann.py [--days N] [--limit N] [--dry]
"""
import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from io import BytesIO

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根目录
KW_PATH = os.path.join(BASE, "AI_KEYWORDS.json")   # 已废弃（词库搬到 a-share-keywords 仓库），留着只是避免老引用报错
STATE_PATH = os.path.join(BASE, "state.json")
FEED_DIR = os.path.join(BASE, "feed")
FEED_PATH = os.path.join(FEED_DIR, "rss.xml")

# 词库唯一真源：a-share-keywords 仓库（每周一自动补词，按行业分文件 + 各自 enabled 开关）。
# 流程：取 index.json 拿行业文件名 → 逐个取 enabled=true 的合并。取不到就本轮跳过（不用过期副本）。
KW_INDEX = ["https://cdn.jsdelivr.net/gh/liuchao88/a-share-keywords@main/keywords/index.json",
            "https://raw.githubusercontent.com/liuchao88/a-share-keywords/main/keywords/index.json"]
KW_BASE = ["https://cdn.jsdelivr.net/gh/liuchao88/a-share-keywords@main/keywords/",
           "https://raw.githubusercontent.com/liuchao88/a-share-keywords/main/keywords/"]

# 公告里到处是模板语言的强档词：降级处理（单命中不算，要和别的词一起才过关）
# 实测："重组"曾让 122 条命中里 50 条单靠它过关（不构成重大资产重组 / 关联交易 / 债务重组 / 重组疫苗…）
DEMOTE_STRONG = {"重组", "并购", "采购"}

TZ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122 Safari/537.36"
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE

FEED_TITLE = "巨潮公告 · 关键词命中（AI产业链）"
FEED_LINK = "http://www.cninfo.com.cn/"
FEED_DESC = "全市场公告按标题事件词粗筛 + 正文词库匹配（critical/high 单命中；medium/low 需≥2词）"
MAX_PDF = 600          # 单轮最多下载多少份 PDF（保险丝）
MAX_PAGES = 80         # 单份 PDF 最多抽多少页（超长报告截断）
MAX_MB = 20            # 单份 PDF 最大下载体积
WINDOW_DAYS = 3        # 每次抓最近几天（巨潮按天查不稳，必须重叠兜底）
RELATION_ENABLE = True # 同时抓「调研记录表」（巨潮 调研 tab；2026-10-01 加）
RELATION_FILTER = True # True=调研也要过词库（实测 69% 命中，约 17 条/天）；False=调研全部进 feed（约 25 条/天）
RELATION_SKIP_RE = re.compile(r"与会清单|参会人员|参会名单|名单附件")   # 同一场调研的附件，剔掉留正文那份

# 标题粗筛：通用事件词（不含主题词，主题词交给正文匹配）
EVENT_WORDS = ["定点", "中标", "合同", "订单", "供货", "供货协议", "投资", "收购", "并购", "重组",
               "扩产", "产能", "量产", "投产", "涨价", "提价", "签署", "协议", "合作", "设立",
               "增资", "竞得", "产能建设", "项目", "募投", "框架协议", "股份回购", "增持", "减持",
               "取得", "获得", "突破", "认证", "专利", "研发", "开工", "下线", "出货", "交付"]
EVENT_RE = re.compile("|".join(re.escape(w) for w in EVENT_WORDS))
SKIP_TITLE_RE = re.compile(r"^\s*\d+\s*[-－]\s*\d+")      # 3-2-1 评估报告… 这种附件分片

# 公告里的固定样板句/否定句：先剔掉再匹配，否则"不构成重大资产重组""不涉及关联交易"
# 这类免责声明会让几乎所有公告都命中"重组""并购"（实测 46% 通过率里大半是这种噪音）
BOILER_PATTERNS = [
    r"[^。；\n]{0,40}不(构成|涉及|存在|属于)[^。；\n]{0,40}(重大资产重组|重组上市|关联交易|借壳|要约收购|重大事项)[^。；\n]{0,60}",
    r"[^。；\n]{0,40}(是否)?(构成|涉及)[^。；\n]{0,20}(重大资产重组|重组上市|关联交易)[^。；\n]{0,60}",
    r"(本公司|公司|上市公司)及(董事会)?全体成员保证[^。\n]{0,160}",
    r"[^。；\n]{0,30}(不存在|未有)[^。；\n]{0,40}(重大资产重组|重组|关联交易)[^。；\n]{0,40}",
]
BOILER_RE = re.compile("|".join(BOILER_PATTERNS))


def strip_boiler(text):
    return BOILER_RE.sub(" ", text)


def log(msg):
    print("[%s] %s" % (datetime.now(TZ).strftime("%H:%M:%S"), msg), flush=True)


def http(url, data=None, headers=None, timeout=60, retry=3):
    h = {"User-Agent": UA, "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"}
    if headers:
        h.update(headers)
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=CTX))
    last = None
    for i in range(retry):
        try:
            return op.open(urllib.request.Request(url, data=data, headers=h), timeout=timeout).read()
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def day_list(plate, date, tab="fulltext"):
    """拿某天某市场的公告（翻页，巨潮每次最多给 30 条）。
       tab="fulltext" = 正式公告；tab="relation" = 调研记录表（巨潮页面上的「调研」页，2026-10-01 实测）"""
    out, page = [], 1
    while page <= 60:
        p = dict(plate=plate, column=("szse" if plate == "sz" else "sse"), tabName=tab, stock="",
                 searchkey="", secid="", category="", trade="", sortName="", sortType="",
                 isHLtitle="true", pageNum=page, pageSize=30, seDate=date + "~" + date)
        try:
            d = json.loads(http("http://www.cninfo.com.cn/new/hisAnnouncement/query",
                                data=urllib.parse.urlencode(p).encode(),
                                headers={"Content-Type": "application/x-www-form-urlencoded",
                                         "X-Requested-With": "XMLHttpRequest"}).decode("utf-8", "replace"))
        except Exception as e:
            log("  查询失败 %s %s p%d: %s" % (plate, date, page, e)); break
        ann = d.get("announcements") or []
        out += ann
        if not ann or len(out) >= int(d.get("totalAnnouncement") or 0):
            break
        page += 1
        time.sleep(0.3)
    return out


def merge_dicts(dicts):
    """把多份行业词库合成 (强档, 弱档, 降级词)"""
    strong, weak = set(), set()
    for D in dicts:
        sw = D.get("signal_weights") or {}
        strong |= {w.strip() for w in (sw.get("critical") or []) if w.strip()}
        strong |= {w.strip() for w in (sw.get("high") or []) if w.strip()}
        weak |= {w.strip() for w in (sw.get("medium") or []) if w.strip()}
        weak |= {w.strip() for w in (sw.get("low") or []) if w.strip()}
        for c in D.get("categories") or []:
            for w in (c.get("keywords") or []):
                if w.strip(): weak.add(w.strip())
            for s in (c.get("subcategories") or []):
                for w in (s.get("keywords") or []):
                    if w.strip(): weak.add(w.strip())
        for w in (D.get("entities") or []):
            if w.strip(): weak.add(w.strip())
    demoted = strong & DEMOTE_STRONG             # 模板词降级：自己不算强档，最多算 1 个弱档名额
    strong -= demoted
    return strong, weak, demoted


def load_words():
    """读词库：唯一真源是 a-share-keywords 仓库。
       取 index.json → 逐个取 enabled=true 的行业文件 → 合并。
       任何一步拿不到就返回 None（本轮跳过、不抓）：用过期副本筛是"隐性漏"（新词命中的公告会被静默丢掉），
       比这一轮不抓更糟；下一轮会自动补上。"""
    idx = None
    for u in KW_INDEX:
        try:
            idx = json.loads(http(u, timeout=30, retry=1).decode("utf-8"))
            break
        except Exception:
            continue
    if not idx:
        log("index.json 取不到（jsdelivr 与 raw 都不通）→ 本轮跳过")
        return None, None, None
    D, files, off = [], [], []
    for name in idx.get("files") or []:
        got = None
        for base in KW_BASE:
            try:
                got = json.loads(http(base + name, timeout=30, retry=1).decode("utf-8"))
                break
            except Exception:
                continue
        if not got:
            log("  ✗ %s 取不到，跳过" % name)
            continue
        if not got.get("enabled", True):
            off.append(name)
            continue
        D.append(got)
        files.append(name)
    if not D:
        log("没有任何生效的行业词库 → 本轮跳过")
        return None, None, None
    log("词库来源：a-share-keywords（生效 %s%s）" % ("、".join(files),
        ("；跳过 enabled=false 的 " + "、".join(off)) if off else ""))
    return merge_dicts(D)


def is_ascii(w):
    return re.fullmatch(r"[A-Za-z0-9\-\.\+/ ]+", w) is not None


def match(text, strong, weak, demoted):
    """返回 (强档命中, 普通弱档命中, 降级词命中)。
    降级词（重组/并购/采购）：单命中不算，但和别的词一样计入弱档的"≥2 个不同词"——
    用户 2026-09-26 明确要保留纯重组公告（"这也是投资机会"），所以不做名额限制。"""
    def hit(w):
        return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])", text)) if is_ascii(w) else (w in text)
    s = [w for w in strong if hit(w)]
    dm = [w for w in demoted if hit(w)]
    wk = [w for w in weak if w not in s and w not in dm and hit(w)]
    return s, wk, dm


def snippet(text, word, span=60):
    i = text.find(word)
    if i < 0:
        return ""
    a = max(0, i - span); b = min(len(text), i + len(word) + span)
    return re.sub(r"\s+", " ", text[a:b])


def pdf_text(url):
    blob = http(url, timeout=90)
    if len(blob) > MAX_MB * 1024 * 1024:
        raise ValueError("文件过大 %.1fMB" % (len(blob) / 1048576.0))
    from pypdf import PdfReader
    rd = PdfReader(BytesIO(blob))
    txt = "\n".join((p.extract_text() or "") for p in rd.pages[:MAX_PAGES])
    return txt, len(rd.pages)


def check_pdf(x, kind, strong, weak, demoted, results, seen):
    """下载一份 PDF → 抽正文 → 剔样板句 → 过词库 → 命中就加进 results。
       返回 (是否命中, 是否成功下载)。无论成败都把 id 记进 seen，免得下轮重复下载。"""
    title = (x.get("announcementTitle") or "").replace("<em>", "").replace("</em>", "")
    url = "http://static.cninfo.com.cn/" + (x.get("adjunctUrl") or "")
    try:
        txt, pages = pdf_text(url)
    except Exception as e:
        log("  ✗ %s %s | %s" % (x.get("secCode"), title[:26], str(e)[:60]))
        seen.add(x.get("announcementId") or "")
        return False, False
    code = x.get("secCode") or ""
    body = strip_boiler(txt)
    s, wk, dm = match(body, strong, weak, demoted)
    seen.add(x.get("announcementId") or "")
    ok = bool(s) or (len(wk) + len(dm)) >= 2
    if kind == "调研" and not RELATION_FILTER:
        ok = True                                   # 口径开关：调研不过词库、全收
    if not ok:
        return False, True
    tags = s + wk + dm
    for r in results:                               # 同公司同一天、标题高度相似 → 只留一份
        if r["code"] == code and abs(r["time"] - int(x.get("announcementTime") or 0)) < 86400000:
            import difflib
            if difflib.SequenceMatcher(None, r["title"], title, autojunk=False).ratio() >= 0.55:
                return False, True
    results.append({
        "code": code, "name": x.get("secName"), "title": title, "kind": kind,
        "url": url, "time": int(x.get("announcementTime") or 0), "pages": pages,
        "org": x.get("orgId") or "",
        "strong": s[:8], "weak": wk[:8], "demoted": dm[:4],
        "snippet": snippet(txt, tags[0]) if tags else ""})
    log("  ✔[%s] %s %s | %d页 | 强档 %s | 弱档 %s | 降级 %s" % (
        kind, code, title[:30], pages, "、".join(s[:3]) or "无",
        "、".join(wk[:3]) or "无", "、".join(dm[:2]) or "无"))
    return True, True


def xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def write_feed(hits, path):
    """把命中的公告写成 RSS 2.0（新的在前；每条带命中词、正文片段、PDF 链接）"""
    items = sorted(hits, key=lambda x: x.get("time") or 0, reverse=True)[:300]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0"><channel>',
           '<title>%s</title>' % xml_escape(FEED_TITLE),
           '<link>%s</link>' % xml_escape(FEED_LINK),
           '<description>%s</description>' % xml_escape(FEED_DESC),
           '<language>zh-cn</language>',
           '<lastBuildDate>%s</lastBuildDate>' % format_datetime(datetime.now(TZ))]
    for h in items:
        ts = (h.get("time") or 0) / 1000.0
        dt = datetime.fromtimestamp(ts, TZ) if ts else datetime.now(TZ)
        tags = (h.get("strong") or []) + (h.get("weak") or []) + (h.get("demoted") or [])
        kind = h.get("kind") or "公告"
        title = "[%s%s][%s] %s%s" % (h.get("name") or "", h.get("code") or "", "、".join(tags[:3]),
                                     ("调研 · " if kind == "调研" else ""), h.get("title") or "")
        desc = "命中词：%s%s%s\n%s\n原文 PDF：%s" % (
            "、".join(h.get("strong") or []) or "无",
            ("｜" + "、".join(h.get("weak") or [])) if h.get("weak") else "",
            ("｜降级词：" + "、".join(h.get("demoted") or [])) if h.get("demoted") else "",
            h.get("snippet") or "", h.get("url") or "")
        if h.get("org"):     # 巨潮个股页（点公司名直达该公司公告列表）；wecom_push 从这行里取 orgId
            desc += "\n公司页：http://www.cninfo.com.cn/new/disclosure/stock?stockCode=%s&orgId=%s" % (
                h.get("code") or "", h.get("org"))
        out.append("<item>")
        out.append("<title>%s</title>" % xml_escape(title))
        out.append("<link>%s</link>" % xml_escape(h.get("url") or FEED_LINK))
        out.append("<guid isPermaLink=\"false\">%s</guid>" % xml_escape("%s-%s" % (h.get("code"), h.get("time"))))
        out.append("<pubDate>%s</pubDate>" % format_datetime(dt))
        out.append("<description>%s</description>" % xml_escape(desc))
        out.append("</item>")
    out.append("</channel></rss>")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    log("feed 已写：%s（%d 条）" % (path, len(items)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--limit", type=int, default=MAX_PDF)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--feed-only", action="store_true", help="只按 state.json 重新生成 feed，不抓取")
    a = ap.parse_args()

    if a.feed_only:
        st = json.load(open(STATE_PATH, encoding="utf-8"))
        write_feed(st.get("hits") or [], FEED_PATH)
        return 0

    strong, weak, demoted = load_words()
    if strong is None:
        log("词库不可用 → 这一轮不抓（下一轮自动补；不用旧词库硬筛）")
        return 0
    log("词库：强档 %d 词 / 弱档 %d 词 / 降级词 %d 个（%s）" % (len(strong), len(weak), len(demoted), "、".join(sorted(demoted))))

    state = {"seen": [], "hits": []}
    if os.path.exists(STATE_PATH):
        try:
            state = json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            pass
    seen = set(state.get("seen") or [])

    today = datetime.now(TZ).date()
    days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(a.days)]
    anns, bydate = [], {}
    for d in days:
        for plate in ("sz", "sh"):
            lst = day_list(plate, d)
            bydate["%s-%s" % (d, plate)] = len(lst)
            anns += lst
    uniq = {}
    for x in anns:
        uniq[x.get("announcementId") or x.get("adjunctUrl")] = x
    anns = list(uniq.values())
    log("窗口 %s：抓到 %d 条公告（去重后）" % ("、".join(days), len(anns)))

    cand = [x for x in anns if EVENT_RE.search(x.get("announcementTitle") or "")
            and not SKIP_TITLE_RE.match(x.get("announcementTitle") or "")]
    new = [x for x in cand if (x.get("announcementId") or "") not in seen]
    log("标题含事件词 %d 条 → 其中没处理过的 %d 条" % (len(cand), len(new)))

    results, scanned, failed = [], 0, 0
    for x in new[:a.limit]:
        hit, got = check_pdf(x, "公告", strong, weak, demoted, results, seen)
        scanned += 1 if got else 0
        failed += 0 if got else 1
        time.sleep(0.2)

    # ---- 调研记录表（2026-10-01 新增）：标题里通常没有事件词，不粗筛，直接下 PDF 过词库 ----
    if RELATION_ENABLE:
        rels, uniqr = [], {}
        for d in days:
            for plate in ("sz", "sh"):
                lst = day_list(plate, d, tab="relation")
                if lst:
                    log("  调研 %s %s：%d 条" % (d, plate, len(lst)))
                rels += lst
        for x in rels:
            uniqr[x.get("announcementId") or x.get("adjunctUrl")] = x
        rels = [x for x in uniqr.values()
                if not RELATION_SKIP_RE.search(x.get("announcementTitle") or "")
                and (x.get("announcementId") or "") not in seen]
        log("调研记录表 %d 条（已剔附件清单、去掉处理过的）" % len(rels))
        r0 = len(results)
        for x in rels[:a.limit]:
            hit, got = check_pdf(x, "调研", strong, weak, demoted, results, seen)
            scanned += 1 if got else 0
            failed += 0 if got else 1
            time.sleep(0.2)
        log("调研通过 %d 条" % (len(results) - r0))

    log("=" * 60)
    log("下载 %d 份 PDF（失败 %d）→ 通过筛选 %d 条，通过率 %.0f%%" % (scanned, failed, len(results),
                                                                 100.0 * len(results) / max(1, scanned)))
    log("按这个窗口折算：约 %.0f 条/天" % (len(results) / max(1, a.days)))
    for r in results[:25]:
        log("   %s %s | %s | 强:%s" % (r["code"], r["name"], r["title"][:40], "、".join(r["strong"][:3])))

    if not a.dry:
        os.makedirs(FEED_DIR, exist_ok=True)
        old = state.get("hits") or []
        merged, keys = [], set()
        for h in results + old:                      # 本轮 + 历史，按 (代码, 时间, 标题) 去重
            k = (h.get("code"), h.get("time"), (h.get("title") or "")[:30])
            if k in keys:
                continue
            keys.add(k); merged.append(h)
        json.dump({"seen": list(seen)[-4000:], "hits": merged[:2000],
                   "updated": datetime.now(TZ).isoformat()},
                  open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False)
        log("state.json 已写：seen %d 条 / hits %d 条" % (len(seen), len(merged)))
        write_feed(merged, FEED_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
