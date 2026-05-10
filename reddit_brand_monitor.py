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

def _reddit_get(path, params=None):
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(f"{BASE_URL}{path}", params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    time.sleep(0.6)
    return resp.json()

def get_recent_posts(lookback_days, max_posts=500):
    posts, after = [], None
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
    while len(posts) < max_posts:
        params = {"limit": 100}
        if after:
            params["after"] = after
        data = _reddit_get(f"/r/{SUBREDDIT}/new.json", params)
        children = data.get("data", {}).get("children", [])
        if not children:
            break
        oldest = children[-1]["data"].get("created_utc", 0)
        posts.extend(children)
        if oldest < cutoff_ts:
            break
        after = data.get("data", {}).get("after")
        if not after:
            break
    return posts

def get_recent_comments(lookback_days, max_comments=500):
    comments, after = [], None
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
    while len(comments) < max_comments:
        params = {"limit": 100}
        if after:
            params["after"] = after
        data = _reddit_get(f"/r/{SUBREDDIT}/comments.json", params)
        children = data.get("data", {}).get("children", [])
        if not children:
            break
        oldest = children[-1]["data"].get("created_utc", 0)
        comments.extend(children)
        if oldest < cutoff_ts:
            break
        after = data.get("data", {}).get("after")
        if not after:
            break
    return comments

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
