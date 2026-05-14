"""
Asset Owner Radar — daily harvester.

Workflow:
1. Load source list from data/sources.json
2. For each source: fetch RSS feed or HTML page, extract recent items
3. Deduplicate against previous run (data/seen.json keeps URL hashes)
4. Send new items to Claude in batches for:
   - China-relevance scoring (0-10)
   - Chinese summary (<= 150 chars)
   - Categorization (持仓变动/高管表态/政策动向/市场异动/其他)
5. Filter: keep only score >= 5
6. Generate daily digest from top items
7. Write to public/data/latest.json
8. Also append to public/data/archive/YYYY-MM-DD.json

Designed to be robust: source failures are logged but don't break the run.
"""

import os
import sys
import json
import time
import hashlib
import datetime as dt
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PUBLIC_DATA_DIR = ROOT / "public" / "data"
PUBLIC_DATA_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR = PUBLIC_DATA_DIR / "archive"
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

SOURCES_FILE = DATA_DIR / "sources.json"
SEEN_FILE = DATA_DIR / "seen.json"
LATEST_FILE = PUBLIC_DATA_DIR / "latest.json"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    print("ERROR: ANTHROPIC_API_KEY env var not set", file=sys.stderr)
    sys.exit(1)

client = Anthropic(api_key=ANTHROPIC_API_KEY)

UA = "Mozilla/5.0 (compatible; AssetOwnerRadar/1.0; +https://github.com)"
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}

MAX_ITEMS_PER_SOURCE = 8
LOOKBACK_DAYS = 7
MAX_TOTAL_ITEMS = 200
MIN_SCORE_TO_KEEP = 5.0
TOP_N_FINAL = 60


def url_hash(u: str) -> str:
    return hashlib.sha1(u.encode("utf-8")).hexdigest()[:16]


def load_json(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"WARN: failed to read {p}: {e}", file=sys.stderr)
    return default


def save_json(p: Path, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_rss(source):
    """Parse an RSS/Atom feed, return list of {title, url, summary, published_at}."""
    items = []
    try:
        feed = feedparser.parse(source["url"])
        for e in feed.entries[:MAX_ITEMS_PER_SOURCE]:
            published = None
            for k in ("published", "updated", "created"):
                if hasattr(e, k + "_parsed") and getattr(e, k + "_parsed"):
                    published = dt.datetime(*getattr(e, k + "_parsed")[:6]).isoformat() + "Z"
                    break
            items.append({
                "title": getattr(e, "title", "")[:300],
                "url": getattr(e, "link", ""),
                "raw_summary": getattr(e, "summary", "")[:1500] if hasattr(e, "summary") else "",
                "published_at": published or dt.datetime.utcnow().isoformat() + "Z",
            })
    except Exception as ex:
        print(f"WARN: rss fetch failed for {source['short']}: {ex}", file=sys.stderr)
    return items


def fetch_page(source):
    """Scrape a news/press page. Look for article-like links with titles."""
    items = []
    try:
        r = requests.get(source["url"], headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        candidates = []
        for a in soup.find_all("a", href=True):
            txt = " ".join(a.get_text(" ", strip=True).split())
            if len(txt) < 25 or len(txt) > 250:
                continue
            href = a["href"]
            if href.startswith("/"):
                pu = urlparse(source["url"])
                href = f"{pu.scheme}://{pu.netloc}{href}"
            elif not href.startswith("http"):
                continue
            lower = (txt + " " + href).lower()
            if any(b in lower for b in ["cookie", "subscribe", "login", "privacy policy", "terms of use", "sitemap"]):
                continue
            candidates.append({"title": txt, "url": href})

        seen_urls = set()
        for c in candidates:
            if c["url"] in seen_urls:
                continue
            seen_urls.add(c["url"])
            items.append({
                "title": c["title"],
                "url": c["url"],
                "raw_summary": "",
                "published_at": dt.datetime.utcnow().isoformat() + "Z",
            })
            if len(items) >= MAX_ITEMS_PER_SOURCE:
                break
    except Exception as ex:
        print(f"WARN: page fetch failed for {source['short']}: {ex}", file=sys.stderr)
    return items


def harvest_all():
    """Fetch from every source, return raw items list."""
    sources = load_json(SOURCES_FILE, {}).get("sources", [])
    seen = load_json(SEEN_FILE, {})
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=LOOKBACK_DAYS)

    new_items = []
    fresh_seen = dict(seen)

    for s in sources:
        print(f"  fetching {s['short']} ({s['type']})...")
        items = fetch_rss(s) if s["type"] == "rss" else fetch_page(s)

        for it in items:
            if not it.get("url") or not it.get("title"):
                continue
            uh = url_hash(it["url"])
            if uh in seen:
                continue
            try:
                pub = dt.datetime.fromisoformat(it["published_at"].replace("Z", ""))
                if pub < cutoff:
                    continue
            except Exception:
                pass

            it["source_short"] = s["short"]
            it["source_name"] = s["name"]
            it["category"] = s["category"]
            it["is_firsthand"] = s.get("is_firsthand", False)
            it["country"] = s.get("country", "")
            new_items.append(it)
            fresh_seen[uh] = it["published_at"]

        time.sleep(0.5)

    cutoff_seen = (dt.datetime.utcnow() - dt.timedelta(days=60)).isoformat()
    fresh_seen = {k: v for k, v in fresh_seen.items() if v > cutoff_seen}
    save_json(SEEN_FILE, fresh_seen)

    print(f"  total new items collected: {len(new_items)}")
    return new_items[:MAX_TOTAL_ITEMS]


def ai_process_batch(items_batch):
    """Send a batch of items to Claude, get back enriched results."""
    if not items_batch:
        return []

    items_for_prompt = [
        {
            "id": i,
            "title": it["title"],
            "institution": it["source_short"],
            "category": it["category"],
            "raw_excerpt": it.get("raw_summary", "")[:500],
        }
        for i, it in enumerate(items_batch)
    ]

    prompt = f"""You are an investment intelligence analyst at China Life Asset Management. Analyze the following news items about global Asset Owners (sovereign wealth funds, pensions, insurers, central banks). For each item, evaluate its relevance to China-investment topics.

For each item, return:
- "id": same as input
- "score": China-relevance score 0-10, where:
  * 9-10 = directly about institution's China allocation, holdings, or strategy
  * 7-8 = institution discussing emerging markets / Asia / specific Chinese assets
  * 5-6 = global allocation news that materially affects China exposure
  * 3-4 = tangential (e.g. macro views that mention China)
  * 0-2 = irrelevant to China investing
- "summary": 80-130 character Chinese summary (简体中文), neutral tone, factual. Do NOT copy original wording — paraphrase entirely.
- "type": one of "持仓变动" / "高管表态" / "政策动向" / "市场异动" / "其他"
- "skip": true if score < 5 OR title looks like junk (cookie notice, sitemap, generic page)

Return a JSON array. No markdown, no commentary, just the JSON.

Input items:
{json.dumps(items_for_prompt, ensure_ascii=False, indent=2)}
"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        results = json.loads(text)
        enriched = []
        for r in results:
            if r.get("skip"):
                continue
            idx = r.get("id")
            if idx is None or idx >= len(items_batch):
                continue
            base = items_batch[idx]
            enriched.append({
                "title": base["title"],
                "url": base["url"],
                "institution": base["source_short"],
                "source": base["source_name"],
                "category": base["category"],
                "is_firsthand": base["is_firsthand"],
                "country": base.get("country", ""),
                "published_at": base["published_at"],
                "score": float(r.get("score", 0)),
                "summary": r.get("summary", "")[:300],
                "type": r.get("type", "其他"),
            })
        return enriched
    except Exception as ex:
        print(f"WARN: AI processing failed for batch: {ex}", file=sys.stderr)
        return []


def ai_generate_digest(top_items):
    """Generate a daily digest paragraph from top items."""
    if not top_items:
        return ""

    digest_input = [
        {"机构": it["institution"], "类型": it["type"], "摘要": it["summary"]}
        for it in top_items[:15]
    ]

    prompt = f"""你是国寿资产的研究员。基于以下今日全球资产所有者动态，撰写一段 200-300 字的"今日要闻"摘要，面向投资同事。

要求:
- 用中文，简体
- 按重要性组织，不是简单罗列
- 突出和中国投资相关的关键变化或观点
- 保持客观中立，不做投资建议
- 自然分段，2-4 段
- 不要 markdown 格式，不要标题，纯段落

今日动态:
{json.dumps(digest_input, ensure_ascii=False, indent=2)}
"""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as ex:
        print(f"WARN: digest generation failed: {ex}", file=sys.stderr)
        return ""


def main():
    print("=== Asset Owner Radar — harvest start ===")
    print(f"  timestamp: {dt.datetime.utcnow().isoformat()}Z")

    raw_items = harvest_all()
    if not raw_items:
        print("  no new items this run.")
        existing = load_json(LATEST_FILE, None)
        if existing:
            existing["updated_at"] = dt.datetime.utcnow().isoformat() + "Z"
            save_json(LATEST_FILE, existing)
        return

    print(f"=== AI processing {len(raw_items)} items ===")
    processed = []
    BATCH = 20
    for i in range(0, len(raw_items), BATCH):
        batch = raw_items[i:i + BATCH]
        print(f"  batch {i // BATCH + 1}/{(len(raw_items) + BATCH - 1) // BATCH}...")
        processed.extend(ai_process_batch(batch))
        time.sleep(1)

    processed = [p for p in processed if p["score"] >= MIN_SCORE_TO_KEEP]
    processed.sort(key=lambda x: (x["score"], x["published_at"]), reverse=True)
    processed = processed[:TOP_N_FINAL]

    print(f"=== {len(processed)} items kept after scoring ===")

    prev = load_json(LATEST_FILE, {"items": []})
    prev_items = prev.get("items", [])
    prev_urls = {it["url"] for it in prev_items}

    merged = list(processed)
    for it in prev_items:
        if it["url"] not in {p["url"] for p in merged}:
            try:
                pub = dt.datetime.fromisoformat(it["published_at"].replace("Z", ""))
                if pub > dt.datetime.utcnow() - dt.timedelta(days=LOOKBACK_DAYS):
                    merged.append(it)
            except Exception:
                pass

    merged.sort(key=lambda x: (x["score"], x["published_at"]), reverse=True)
    merged = merged[:TOP_N_FINAL]

    print("=== generating daily digest ===")
    digest = ai_generate_digest([m for m in merged if m["score"] >= 7])

    output = {
        "updated_at": dt.datetime.utcnow().isoformat() + "Z",
        "source_count": len(set(it["institution"] for it in merged)),
        "digest": digest,
        "items": merged,
    }

    save_json(LATEST_FILE, output)
    today = dt.date.today().isoformat()
    save_json(ARCHIVE_DIR / f"{today}.json", output)

    print(f"=== done. {len(merged)} items written to {LATEST_FILE} ===")


if __name__ == "__main__":
    main()
