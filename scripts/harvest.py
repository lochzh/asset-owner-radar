"""
Asset Owner Radar — daily harvester.
"""

import os
import re
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

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}

MAX_ITEMS_PER_SOURCE = 8
LOOKBACK_DAYS = 30
MAX_TOTAL_ITEMS = 250
MIN_SCORE_TO_KEEP = 3.5
TOP_N_FINAL = 100
DIGEST_MIN_SCORE = 5.0


def url_hash(u):
    return hashlib.sha1(u.encode("utf-8")).hexdigest()[:16]


def load_json(p, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"WARN: failed to read {p}: {e}", file=sys.stderr)
    return default


def save_json(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_rss(source):
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
    sources = load_json(SOURCES_FILE, {}).get("sources", [])
    seen = load_json(SEEN_FILE, {})
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=LOOKBACK_DAYS)
    new_items = []
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
            it["url_hash"] = uh
            it["source_short"] = s["short"]
            it["source_name"] = s["name"]
            it["category"] = s["category"]
            it["is_firsthand"] = s.get("is_firsthand", False)
            it["country"] = s.get("country", "")
            new_items.append(it)
        time.sleep(0.5)
    print(f"  total new items collected: {len(new_items)}")
    return new_items[:MAX_TOTAL_ITEMS]


def extract_json_array(text):
    """Robustly extract a JSON array from model output, ignoring any markdown fences or prose."""
    text = text.strip()
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        return m.group(0)
    return text


def ai_process_batch(items_batch):
    if not items_batch:
        return [], []
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
    items_json = json.dumps(items_for_prompt, ensure_ascii=False, indent=2)
    prompt = (
        "You are an investment intelligence analyst at China Life Asset Management. "
        "Analyze news items about global Asset Owners (sovereign wealth funds, pensions, insurers, central banks).\n\n"
        "Score each item 0-10 for relevance to a Chinese institutional investor's perspective. Be GENEROUS — "
        "we want broad market intelligence, not just direct China mentions:\n\n"
        "* 8-10: Directly about institution's China allocation, Chinese assets, China strategy, Hong Kong, or specific Chinese sectors\n"
        "* 6-7: Asia-Pacific, emerging markets, RMB, Asian equities/credit, or institutional moves with clear Asia/China implications\n"
        "* 4-5: Global allocation strategy, portfolio rebalancing, EM/DM rotation, macro views, geopolitics, central bank policy — "
        "anything that could materially shape capital flows including to China\n"
        "* 2-3: Generic institutional news (leadership, governance, sustainability) at systemically important asset owners\n"
        "* 0-1: Clearly irrelevant (local administrative news, philanthropy without investment angle, cookie/navigation/junk)\n\n"
        "When uncertain, score HIGHER. Chinese institutional investors track the global allocation context broadly. "
        "A major sovereign fund discussing tech investment strategy, even without mentioning China, is signal.\n\n"
        "For each item return JSON with these keys: id, score (0-10 float), summary (80-130 char 简体中文 paraphrase), "
        "type (one of: 持仓变动, 高管表态, 政策动向, 市场异动, 其他), skip (true ONLY for clear junk like cookie banners, navigation, sitemaps).\n\n"
        "Return ONLY a JSON array, no markdown code fences, no commentary.\n\n"
        "Input items:\n" + items_json
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = resp.content[0].text
        text = extract_json_array(raw_text)
        results = json.loads(text)
        enriched = []
        processed_hashes = []
        for r in results:
            idx = r.get("id")
            if idx is None or idx >= len(items_batch):
                continue
            base = items_batch[idx]
            processed_hashes.append((base["url_hash"], base["published_at"]))
            if r.get("skip"):
                continue
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
        return enriched, processed_hashes
    except Exception as ex:
        print(f"WARN: AI processing failed for batch: {ex}", file=sys.stderr)
        try:
            print(f"  first 200 chars of response: {raw_text[:200] if 'raw_text' in dir() else 'no response'}", file=sys.stderr)
        except Exception:
            pass
        return [], []


def ai_generate_digest(top_items):
    if not top_items:
        return ""
    digest_input = [
        {"机构": it["institution"], "类型": it["type"], "摘要": it["summary"]}
        for it in top_items[:15]
    ]
    items_json = json.dumps(digest_input, ensure_ascii=False, indent=2)
    prompt = (
        "你是国寿资产的研究员。基于以下近期全球资产所有者动态，撰写一段 200-300 字的近期要闻摘要，面向投资同事。\n\n"
        "要求:\n- 用中文，简体\n- 按重要性组织，不是简单罗列\n"
        "- 突出和中国投资、亚洲、新兴市场相关的关键变化或观点\n"
        "- 保持客观中立，不做投资建议\n- 自然分段，2-4 段\n"
        "- 不要 markdown 格式，不要标题，纯段落\n\n"
        "近期动态:\n" + items_json
    )
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
    print(f"  LOOKBACK_DAYS={LOOKBACK_DAYS}, MIN_SCORE_TO_KEEP={MIN_SCORE_TO_KEEP}")
    raw_items = harvest_all()
    processed = []
    successfully_processed_hashes = []
    if raw_items:
        print(f"=== AI processing {len(raw_items)} items ===")
        BATCH = 20
        for i in range(0, len(raw_items), BATCH):
            batch = raw_items[i:i + BATCH]
            print(f"  batch {i // BATCH + 1}/{(len(raw_items) + BATCH - 1) // BATCH}...")
            enriched, hashes = ai_process_batch(batch)
            processed.extend(enriched)
            successfully_processed_hashes.extend(hashes)
            time.sleep(1)
        seen = load_json(SEEN_FILE, {})
        for uh, pub_at in successfully_processed_hashes:
            seen[uh] = pub_at
        cutoff_seen = (dt.datetime.utcnow() - dt.timedelta(days=90)).isoformat()
        seen = {k: v for k, v in seen.items() if v > cutoff_seen}
        save_json(SEEN_FILE, seen)
        print(f"  seen.json updated: {len(seen)} URLs tracked")
    processed = [p for p in processed if p["score"] >= MIN_SCORE_TO_KEEP]
    print(f"=== {len(processed)} new items kept after scoring ===")
    prev = load_json(LATEST_FILE, {"items": []})
    prev_items = prev.get("items", [])
    print(f"  previous run had {len(prev_items)} items")
    merged = list(processed)
    existing_urls = {p["url"] for p in merged}
    keep_cutoff = dt.datetime.utcnow() - dt.timedelta(days=LOOKBACK_DAYS)
    for it in prev_items:
        if it["url"] in existing_urls:
            continue
        try:
            pub = dt.datetime.fromisoformat(it["published_at"].replace("Z", ""))
            if pub > keep_cutoff:
                merged.append(it)
        except Exception:
            pass
    merged.sort(key=lambda x: (x["score"], x["published_at"]), reverse=True)
    merged = merged[:TOP_N_FINAL]
    print(f"  merged total: {len(merged)} items")
    print("=== generating daily digest ===")
    digest = ai_generate_digest([m for m in merged if m["score"] >= DIGEST_MIN_SCORE])
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
