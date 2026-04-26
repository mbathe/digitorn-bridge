# /dive — Deep dive on one topic in one feed

Usage: `/dive <feed_url> <topic_keyword>`

Steps :
1. Re-fetch the feed via `http.get`.
2. Filter items whose title OR summary contains the topic keyword (case-insensitive, FR/EN).
3. Return up to 5 items with **expanded** summaries (3–5 sentences each, derived from `<description>`/`<content>`).
4. End with "Suggested follow-ups: <bullet list of related search queries>".

If 0 items match, say so and suggest broader keywords found in the feed.
