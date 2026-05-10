#!/usr/bin/env python3
"""
Curador Brands — Reddit Brand Monitor
======================================
Searches r/MissouriMedical for mentions of Curador brands,
categorizes each mention, drafts follow-up responses where needed,
and posts a formatted report to Slack #ai-reddit-brand-mentions.

Environment variables required:
  SLACK_WEBHOOK_URL     — Slack incoming webhook URL
  ANTHROPIC_API_KEY     — Anthropic API key (for categorization + response drafts)
  LOOKBACK_DAYS         — (optional) days to look back; defaults to 1
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone

SUBREDDIT  = "MissouriMedical"
BASE_URL   = "https://www.reddit.com"
USER_AGENT = "script:CuradorBrandMonitor:v1.1 (by /u/curadorbrands)"

BRAND_TERMS = {
    "HeadChange": ["head change", "headchange"],
    "SafeBet":    ["safe bet", "safebet", "safe-bet"],
    "Bubbles":    ["bubbles vape", "bubbles cart", "bubbles cannabis"],
    "Airo":       ["airo", "airopro", "airo pro", "airo brands", "airobrands"],
    "Curador":    ["curador", "curador brands", "curador labs", "curador holdings"],
}

import xml.etree.ElementTree as ET

def _parse_rss_date(s):
    for fmt in ("%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)

def _fetch_rss(url):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    time.sleep(0.6)
    return ET.fromstring(resp.content)

def _rss_entries_to_items(entries, item_type):
    items = []
    ATOM = "http://www.w3.org/2005/Atom"
    for entry in entries:
        updated = entry.findtext(f"{{{ATOM}}}updated") or ""
        dt = _parse_rss_date(updated)
        author_el = entry.find(f"{{{ATOM}}}author")
        author = (author_el.findtext(f"{{{ATOM}}}name") or "[deleted]").replace("/u/", "") if author_el is not None else "[deleted]"
        link_el = entry.find(f"{{{ATOM}}}link")
        link = (link_el.get("href") or "") if link_el is not None else ""
        title = entry.findtext(f"{{{ATOM}}}title") or ""
        content = entry.findtext(f"{{{ATOM}}}content") or ""
        uid = (entry.findtext(f"{{{ATOM}}}id") or link).rstrip("/").rsplit("/", 2)[-2] if link else ""
        field = "selftext" if item_type == "post" else "body"
        items.append({"data": {
            "id": uid, "title": title if item_type == "post" else "",
            field: content, "author": author,
            "permalink": link.replace("https://www.reddit.com", ""),
            "created_utc": dt.timestamp(), "score": 0,
        }})
    return items

def get_recent_posts(lookback_days, max_posts=500):
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    root = _fetch_rss(f"{BASE_URL}/r/{SUBREDDIT}/new.rss?limit=100")
    ATOM = "http://www.w3.org/2005/Atom"
    entries = root.findall(f".//{{{ATOM}}}entry")
    items = _rss_entries_to_items(entries, "post")
    return [i for i in items if datetime.fromtimestamp(i["data"]["created_utc"], tz=timezone.utc) >= cutoff]

def get_recent_comments(lookback_days, max_comments=500):
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    root = _fetch_rss(f"{BASE_URL}/r/{SUBREDDIT}/comments.rss?limit=100")
    ATOM = "http://www.w3.org/2005/Atom"
    entries = root.findall(f".//{{{ATOM}}}entry")
    items = _rss_entries_to_items(entries, "comment")
    return [i for i in items if datetime.fromtimestamp(i["data"]["created_utc"], tz=timezone.utc) >= cutoff]

def find_mentions(posts, comments, lookback_days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    seen_ids, mentions = set(), []
    def _check(item_type, item_data):
        uid = item_data.get("id", "")
        if uid in seen_ids:
            return
        created = datetime.fromtimestamp(item_data.get("created_utc", 0), tz=timezone.utc)
        if created < cutoff:
            return
        title = item_data.get("title", "") or item_data.get("link_title", "")
        body  = item_data.get("selftext", "") or item_data.get("body", "")
        full_text = f"{title} {body}".lower()
        for brand, terms in BRAND_TERMS.items():
            for term in terms:
                if term.lower() in full_text:
                    seen_ids.add(uid)
                    mentions.append({
                        "type": item_type, "id": uid, "brand": brand, "term_matched": term,
                        "title": title, "body": body[:600],
                        "author": item_data.get("author", "[deleted]"),
                        "url": "https://reddit.com" + item_data.get("permalink", ""),
                        "created": created.strftime("%Y-%m-%d %H:%M UTC"),
                        "score": item_data.get("score", 0),
                    })
                    break
    for p in posts:
        _check("post", p["data"])
    for c in comments:
        _check("comment", c["data"])
    return mentions

CATEGORY_PROMPT = """You are a social media analyst for Curador Brands, a licensed Missouri cannabis company.
Brands: HeadChange (premium extracts/vape carts), SafeBet (value products), Bubbles (vape), Airo (licensed partner).

Analyze this Reddit {item_type} and return ONLY valid JSON.
Brand mentioned: {brand}
Author: u/{author}
Title: {title}
Content: {body}

Return this exact JSON:
{{"category":"<Positive Review|Negative Review|Question/Inquiry|General Mention|Complaint|Competitive Comparison|Neutral Discussion>","sentiment":"<positive|negative|neutral>","needs_followup":<true|false>,"followup_reason":"<reason or null>","draft_response":"<brand-rep reply under 150 words, or null>"}}

Draft response rules: warm and professional, no medical claims, no prices."""

def categorize_mentions(mentions):
    import anthropic as _anthropic
    client, results = _anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]), []
    for m in mentions:
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=600,
                messages=[{"role":"user","content":CATEGORY_PROMPT.format(
                    item_type=m["type"],brand=m["brand"],author=m["author"],
                    title=m["title"] or "(no title)",body=m["body"] or "(no body)")}])
            analysis = json.loads(resp.content[0].text)
        except Exception as e:
            print(f"  Categorization failed for {m['id']}: {e}")
            analysis = {"category":"General Mention","sentiment":"neutral",
                        "needs_followup":False,"followup_reason":None,"draft_response":None}
        results.append({**m, **analysis})
        time.sleep(0.3)
    return results

SEMOJI = {"positive":"🟢","negative":"🔴","neutral":"🟡"}
CEMOJI = {"Positive Review":"⭐","Negative Review":"👎","Question/Inquiry":"❓",
          "Complaint":"⚠️","General Mention":"💬","Competitive Comparison":"⚡","Neutral Discussion":"📝"}

def build_slack_payload(results, date_str, lookback_days):
    period = "24 hours" if lookback_days == 1 else f"{lookback_days} days"
    total, n_fu = len(results), sum(1 for r in results if r.get("needs_followup"))
    blocks = [
        {"type":"header","text":{"type":"plain_text","text":f"🔍 Reddit Brand Monitor — {date_str}"}},
        {"type":"section","text":{"type":"mrkdwn","text":
            f"*Subreddit:* r/MissouriMedical  |  *Period:* last {period}\n*{total} mention(s) found* · *{n_fu} need follow-up*"
            if total else
            f"*Subreddit:* r/MissouriMedical  |  *Period:* last {period}\n✅ No brand mentions found."}}]
    if not total:
        return {"text":f"Reddit Brand Monitor — {date_str}: no mentions found.","blocks":blocks}
    fu = [r for r in results if r.get("needs_followup")]
    if fu:
        blocks += [{"type":"divider"},{"type":"section","text":{"type":"mrkdwn","text":"🚨 *Action Required — Needs Follow-Up*"}}]
        for r in fu:
            s = (r["body"][:250]+"…") if len(r["body"])>250 else r["body"]
            blocks.append({"type":"section","text":{"type":"mrkdwn","text":
                f"{SEMOJI.get(r.get('sentiment','neutral'),'🟡')} {CEMOJI.get(r.get('category','General Mention'),'💬')}  *{r['brand']}*  ·  {r['category']}\nu/{r['author']}  ·  {r['created']}  ·  Score: {r['score']}\n>{s}\n<{r['url']}|View on Reddit>"}})
            if r.get("draft_response"):
                blocks.append({"type":"section","text":{"type":"mrkdwn","text":f"*💬 Suggested Reply:*\n_{r['draft_response']}_"}})
    nf = [r for r in results if not r.get("needs_followup")]
    if nf:
        blocks += [{"type":"divider"},{"type":"section","text":{"type":"mrkdwn","text":"ℹ️ *FYI — No Follow-Up Needed*"}}]
        for r in nf:
            s = (r["body"][:150]+"…") if len(r["body"])>150 else r["body"]
            blocks.append({"type":"section","text":{"type":"mrkdwn","text":
                f"{SEMOJI.get(r.get('sentiment','neutral'),'🟡')} {CEMOJI.get(r.get('category','General Mention'),'💬')}  *{r['brand']}*  ·  {r['category']}\nu/{r['author']}  ·  {r['created']}\n>{s}\n<{r['url']}|View on Reddit>"}})
    blocks += [{"type":"divider"},{"type":"context","elements":[{"type":"mrkdwn","text":"Curador Brand Monitor · Powered by Claude · r/MissouriMedical"}]}]
    return {"text":f"Reddit Brand Monitor — {date_str}: {total} mention(s), {n_fu} need follow-up.","blocks":blocks}

def main():
    lookback_days = int(os.environ.get("LOOKBACK_DAYS","1"))
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    print(f"=== Curador Reddit Brand Monitor — {date_str} ===")
    print(f"Lookback: {lookback_days} day(s) | Subreddit: r/{SUBREDDIT}")
    print("\n[1/4] Fetching recent posts…")
    all_posts = get_recent_posts(lookback_days, max_posts=500)
    print(f"  → {len(all_posts)} post(s) fetched")
    print("\n[2/4] Fetching recent comments…")
    comments = get_recent_comments(lookback_days, max_comments=500)
    print(f"  → {len(comments)} comment(s) fetched")
    print("\n[3/4] Scanning for brand mentions…")
    mentions = find_mentions(all_posts, comments, lookback_days)
    print(f"  → {len(mentions)} unique mention(s) found")
    for m in mentions:
        print(f"     [{m['brand']}] {m['type']} by u/{m['author']}: {m['url']}")
    print("\n[4/4] Categorizing mentions…")
    results = categorize_mentions(mentions) if mentions else []
    fu_count = sum(1 for r in results if r.get("needs_followup"))
    print(f"  → {fu_count} need follow-up")
    print("\nPosting to Slack…")
    payload = build_slack_payload(results, date_str, lookback_days)
    resp = requests.post(os.environ["SLACK_WEBHOOK_URL"], json=payload, timeout=10)
    resp.raise_for_status()
    print("  ✅ Posted to #ai-reddit-brand-mentions")
    print(f"\nDone. {len(results)} mention(s) · {fu_count} need follow-up.")

if __name__ == "__main__":
    main()
