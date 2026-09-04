---
name: social-check
description: Social/X forensics on a token — declared links, account age vs launch, astroturf markers, recent mentions for sentiment reading. Use when the user asks about a token's Twitter/X, socials, community, or sentiment.
---

# Social check

1. Run: `python -m src.onchain.social <TOKEN> --symbol <SYM> --launch-ts <unix>`
   (launch timestamp comes from the launch feeds; omit if unknown).
2. Interpret in this order:
   - **No declared X link** → say so; for a token claiming a community, that
     is itself a flag.
   - **`created_after_token_launch: true`** → account was made for this
     launch. Combined with low tweet_count and low followers, that is the
     farm astroturf pattern from the source thesis ("a few twitter posts to
     make you think the project is active").
   - **Follower/engagement mismatch** — thousands of followers on an account
     days old, or high followers with near-zero engagement on `mentions`,
     suggests bought followers.
3. **Sentiment**: read the raw `mentions` array yourself and summarize the
   actual content — who is posting (bot-like repetition vs real accounts),
   what they claim, and whether engagement is organic. Report a qualitative
   read with examples. Do NOT reduce it to a numeric sentiment score, and do
   not treat volume as positivity: coordinated shilling reads as "high
   sentiment" to any counter.
4. Degradations to state plainly: without `X_BEARER_TOKEN` only declared
   links are checked; X search requires a paid API tier — if `mentions` is
   empty, say whether that means "no chatter" or "no search access".
5. Social signals NEVER override on-chain flags: a great community read on a
   token whose snipers are offloading is marketing on top of an exit.
