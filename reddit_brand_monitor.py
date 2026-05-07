#!/usr/bin/env python3
"""
Curador Brands — Reddit Brand Monitor
======================================
Searches r/MissouriMedical for mentions of Curador brands,
categorizes each mention, drafts follow-up responses where needed,
and posts a formatted report to Slack #ai-reddit-brand-mentions.

Brands monitored: HeadChange, SafeBet, Bubbles, Airo, Curador

Environment variables required:
  SLACK_WEBHOOK_URL   — Slack incoming webhook URL
  ANTHROPIC_API_KEY   — Anthropic API key (for categorization + drafts)
  LOOKBACK_DAYS       — (optional) days to look back; defaults to 1
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUBREDDIT = "MissouriMedical"
USER_AGENT = "CuradorBrandMonitor/1.0 (monitoring for Curador Brands; contact cowens@curadorbrands.com)"

BRAND_TERMS = {
    "HeadChange": ["head change", "headchange"],
    "SafeBet":    ["safe bet", "safebet", "safe-bet"],
    "Bubbles":    ["bubbles vape", "bubbles cart", "bubbles cannabis"],
    "Airo":       ["airo", "airopro", "airo pro", "airo brands", "airobrands"],
    "Curador":    ["curador", "curador brands", "curador labs", "curador holdings"],
}

# ---------------------------------------------------------------------------
# Reddit fetching
# ---------------------------------------------------------------------------

def _reddit_get(url, params=None):
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    time.sleep(0.6)   # Reddit rate limit: ~1 req/sec
    return resp.json()


def search_posts(term: str, lookback_days: int) -> list:
    """Search subreddit posts for a keyword."""
    time_filter = "day" if lookback_days <= 1 else "week" if lookback_days <= 7 else "month"
    data = _reddit_get(
        f"https://www.reddit.com/r/{SUBREDDIT}/search.json",
        params={"q": term, "restrict_sr": "1", "sort": "new", "t": time_filter, "limit": 100},
    )
    return data.get("data", {}).get("children", [])


def get_recent_comments(lookback_days: int, max_comments: int = 500) -> list:
    """Fetch recent comments from the subreddit via pagination."""
    comments = []
    after = None
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()

    while len(comments) < max_comments:
        params = {"limit": 100}
        if after:
            params["after"] = after
        data = _reddit_get(f"https://www.reddit.com/r/{SUBREDDIT}/comments.json", params)
        children = data.get("data", {}).get("children", [])
        if not children:
            break
        # Stop paginating once we've passed the cutoff
        oldest = children[-1]["data"].get("created_utc", 0)
        comments.extend(children)
        if oldest < cutoff_ts:
            break
        after = data.get("data", {}).get("after")
        if not after:
            break

    return comments

# ---------------------------------------------------------------------------
# Mention detection
# ---------------------------------------------------------------------------

def find_mentions(posts: list, comments: list, lookback_days: int) -> list:
    """Return de-duplicated list of brand mentions from posts + comments."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    seen_ids = set()
    mentions = []

    def _check(item_type, item_data):
        uid = item_data.get("id", "")
        if uid in seen_ids:
            return
        created_ts = item_data.get("created_utc", 0)
        created = datetime.fromtimestamp(created_ts, tz=timezone.utc)
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
                        "type":         item_type,
                        "id":           uid,
                        "brand":        brand,
                        "term_matched": term,
                        "title":        title,
                        "body":         body[:600],
                        "author":       item_data.get("author", "[deleted]"),
                        "url":          "https://reddit.com" + item_data.get("permalink", ""),
                        "created":      created.strftime("%Y-%m-%d %H:%M UTC"),
                        "score":        item_data.get("score", 0),
                    })
                    break  # one match per brand per item

    for p in posts:
        _check("post", p["data"])
    for c in comments:
        _check("comment", c["data"])

    return mentions

# ---------------------------------------------------------------------------
# Categorization + response drafting (Claude Haiku)
# ---------------------------------------------------------------------------

CATEGORY_PROMPT = """You are a social media analyst for Curador Brands, a licensed Missouri cannabis company.
Brands: HeadChange (premium extracts/vape carts), SafeBet (accessible value products), Bubbles (vape), Airo (licensed partner brand).

Analyze this Reddit {item_type} and return ONLY valid JSON — no markdown, no extra text.

Brand mentioned: {brand}
Author: u/{author}
Title: {title}
Content: {body}

Return this exact JSON structure:
{{
  "category": "<one of: Positive Review | Negative Review | Question/Inquiry | General Mention | Complaint | Competitive Comparison | Neutral Discussion>",
  "sentiment": "<positive | negative | neutral>",
  "needs_followup": <true | false>,
  "followup_reason": "<brief reason if needs_followup is true, otherwise null>",
  "draft_response": "<brand-rep response if needs_followup is true, otherwise null>"
}}

Rules for draft_response:
- Write as a real Curador/brand team member — warm, helpful, professional
- For questions: answer helpfully or direct to dispensary/website
- For complaints: acknowledge, empathize, offer to help resolve
- For negative reviews: thank for feedback, invite follow-up
- Do NOT make medical claims or guarantees
- Keep under 150 words
- Do NOT mention specific prices"""


def categorize_mentions(mentions: list) -> list:
    import anthropic as _anthropic
    client = _anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    results = []

    for m in mentions:
        prompt = CATEGORY_PROMPT.format(
            item_type=m["type"],
            brand=m["brand"],
            author=m["author"],
            title=m["title"] or "(no title)",
            body=m["body"] or "(no body)",
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            analysis = json.loads(resp.content[0].text)
        except Exception as e:
            print(f"  ⚠️  Categorization failed for {m['id']}: {e}")
            analysis = {
                "category": "General Mention",
                "sentiment": "neutral",
                "needs_followup": False,
                "followup_reason": None,
                "draft_response": None,
            }
        results.append({**m, **analysis})
        time.sleep(0.3)

    return results

# ---------------------------------------------------------------------------
# Slack formatting
# ---------------------------------------------------------------------------

SENTIMENT_EMOJI = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}
CATEGORY_EMOJI  = {
    "Positive Review":       "⭐",
    "Negative Review":       "👎",
    "Question/Inquiry":      "❓",
    "Complaint":             "⚠️",
    "General Mention":       "💬",
    "Competitive Comparison":"⚡",
    "Neutral Discussion":    "📝",
}


def build_slack_payload(results: list, date_str: str, lookback_days: int) -> dict:
    period = "24 hours" if lookback_days == 1 else f"{lookback_days} days"
    total  = len(results)
    n_fu   = sum(1 for r in results if r.get("needs_followup"))

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🔍 Reddit Brand Monitor — {date_str}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Subreddit:* r/MissouriMedical  |  *Period:* last {period}\n"
                    f"*{total} mention(s) found* · *{n_fu} need follow-up*"
                    if total else
                    f"*Subreddit:* r/MissouriMedical  |  *Period:* last {period}\n"
                    f"✅ No brand mentions found."
                ),
            },
        },
    ]

    if not total:
        return {"text": f"Reddit Brand Monitor — {date_str}: no mentions found.", "blocks": blocks}

    # --- Needs follow-up ---
    fu_items = [r for r in results if r.get("needs_followup")]
    if fu_items:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "🚨 *Action Required — Needs Follow-Up*"},
        })
        for r in fu_items:
            se  = SENTIMENT_EMOJI.get(r.get("sentiment", "neutral"), "🟡")
            ce  = CATEGORY_EMOJI.get(r.get("category", "General Mention"), "💬")
            snippet = (r["body"][:250] + "…") if len(r["body"]) > 250 else r["body"]
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{se} {ce}  *{r['brand']}*  ·  {r['category']}\n"
                        f"u/{r['author']}  ·  {r['created']}  ·  Score: {r['score']}\n"
                        f">{snippet}\n"
                        f"<{r['url']}|View on Reddit>"
                    ),
                },
            })
            if r.get("draft_response"):
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*💬 Suggested Reply:*\n_{r['draft_response']}_",
                    },
                })

    # --- No follow-up needed ---
    nf_items = [r for r in results if not r.get("needs_followup")]
    if nf_items:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "ℹ️ *FYI — No Follow-Up Needed*"},
        })
        for r in nf_items:
            se  = SENTIMENT_EMOJI.get(r.get("sentiment", "neutral"), "🟡")
            ce  = CATEGORY_EMOJI.get(r.get("category", "General Mention"), "💬")
            snippet = (r["body"][:150] + "…") if len(r["body"]) > 150 else r["body"]
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{se} {ce}  *{r['brand']}*  ·  {r['category']}\n"
                        f"u/{r['author']}  ·  {r['created']}\n"
                        f">{snippet}\n"
                        f"<{r['url']}|View on Reddit>"
                    ),
                },
            })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "Curador Brand Monitor · Powered by Claude · r/MissouriMedical"}],
    })

    return {
        "text": f"Reddit Brand Monitor — {date_str}: {total} mention(s), {n_fu} need follow-up.",
        "blocks": blocks,
    }

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "1"))
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    print(f"=== Curador Reddit Brand Monitor — {date_str} ===")
    print(f"Lookback: {lookback_days} day(s) | Subreddit: r/{SUBREDDIT}")

    # 1. Fetch posts for all brand terms
    print("\n[1/4] Searching posts…")
    all_posts = []
    for brand, terms in BRAND_TERMS.items():
        for term in terms:
            print(f"  Searching: {term}")
            posts = search_posts(term, lookback_days)
            all_posts.extend(posts)
    print(f"  → {len(all_posts)} raw post result(s)")

    # 2. Fetch recent comments
    print("\n[2/4] Fetching recent comments…")
    comments = get_recent_comments(lookback_days, max_comments=500)
    print(f"  → {len(comments)} comment(s) fetched")

    # 3. Find brand mentions
    print("\n[3/4] Scanning for brand mentions…")
    mentions = find_mentions(all_posts, comments, lookback_days)
    print(f"  → {len(mentions)} unique mention(s) found")

    if mentions:
        for m in mentions:
            print(f"     [{m['brand']}] {m['type']} by u/{m['author']}: {m['url']}")

    # 4. Categorize + draft
    print("\n[4/4] Categorizing mentions…")
    if mentions:
        results = categorize_mentions(mentions)
        fu_count = sum(1 for r in results if r.get("needs_followup"))
        print(f"  → {fu_count} need follow-up")
    else:
        results = []

    # 5. Post to Slack
    print("\nPosting to Slack…")
    payload = build_slack_payload(results, date_str, lookback_days)
    slack_url = os.environ["SLACK_WEBHOOK_URL"]
    resp = requests.post(slack_url, json=payload, timeout=10)
    resp.raise_for_status()
    print("  ✅ Posted to #ai-reddit-brand-mentions")

    print(f"\nDone. {len(results)} mention(s) · {sum(1 for r in results if r.get('needs_followup'))} need follow-up.")


if __name__ == "__main__":
    main()
