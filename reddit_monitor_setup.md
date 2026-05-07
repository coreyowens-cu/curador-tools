# Reddit Brand Monitor — Setup Guide

Everything is built. You need to do three things, ~10 minutes total.

---

## Step 1 — Create a Slack Incoming Webhook (3 min)

This is the URL the script posts to.

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name it `Curador Brand Monitor`, pick your workspace
3. Click **Incoming Webhooks** → toggle it **On**
4. Click **Add New Webhook to Workspace** → select `#ai-reddit-brand-mentions` → Allow
5. Copy the webhook URL — it looks like `https://hooks.slack.com/services/T.../B.../...`

---

## Step 2 — Add to a GitHub Repo (5 min)

Add the two files to any Curador GitHub repo (curadorOS works, or create a new `curador-tools` repo):

```
reddit_brand_monitor.py
.github/workflows/reddit_brand_monitor.yml
```

If you're using an existing repo, just drop both files in — no changes to existing code needed.

---

## Step 3 — Add Two GitHub Secrets (2 min)

In the repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `SLACK_WEBHOOK_URL` | The webhook URL from Step 1 |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |

---

## Run It Right Now (manual trigger)

1. Go to your repo on GitHub
2. Click **Actions** tab
3. Click **Reddit Brand Monitor** in the left sidebar
4. Click **Run workflow** → set `lookback_days` to `30` for the first run → **Run workflow**
5. Results appear in #ai-reddit-brand-mentions within ~2 minutes

---

## What Happens Daily

The workflow runs automatically Monday–Friday at 9 AM CT, Saturday–Sunday at 10 AM CT.
Each run looks back 24 hours. You can always trigger a manual run from the Actions tab.

---

## What the Slack Report Looks Like

Each report includes:
- **🚨 Needs Follow-Up** section — complaints, questions, negative reviews, with a drafted brand-rep response ready to copy-paste
- **ℹ️ FYI** section — positive reviews and general mentions that don't need a reply
- Brand, category, sentiment, author, timestamp, and a direct link to the Reddit thread

Categories: Positive Review · Negative Review · Question/Inquiry · Complaint · General Mention · Competitive Comparison · Neutral Discussion

---

## Troubleshooting

**No results showing up:** Run manually with `lookback_days: 30` to confirm it's working.

**Slack posts not appearing:** Double-check the `SLACK_WEBHOOK_URL` secret is set correctly and the channel is `#ai-reddit-brand-mentions`.

**Script errors in Actions log:** Check that both secrets are set. The `ANTHROPIC_API_KEY` is needed for categorization.
