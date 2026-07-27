"""One-off migration: push the legacy wishkr-web/src/app/lib/blog-posts.ts
post(s) into R2 as JSON, in the shape app/api/admin_blog.py expects, then
rebuild blog/index.json. Run once after deploying the admin blog CMS.

Usage: python scripts/migrate_blog_posts_to_r2.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.admin_blog import _post_key, _put_json, _r2_client, _rebuild_index  # noqa: E402
from app.core.config import settings  # noqa: E402

POSTS = [
    {
        "slug": "when-to-send-a-gift-before-an-occasion",
        "title": "When to Send a Gift Before an Occasion — Timing Guide for India",
        "description": "Learn when to send gifts in India before birthdays, anniversaries, festivals, and weddings, with timing for digital, same-day, and personalised gifts.",
        "heroTitle": "When to Send a Gift Before an Occasion — An India-Specific Timing Guide",
        "heroIntro": "Most gifting mistakes aren't about the gift — they're about the timing. A perfect gift that arrives a day late is a bad gift, and a last-minute replacement that arrives early often beats the 'right' gift sent too late. This guide works through the real delivery windows for every major category in India so you stop guessing.",
        "readingTimeMinutes": 8,
        "publishedOn": "2026-04-24",
        "updatedOn": None,
        "author": "Wishkr",
        "category": "pillar",
        "pillarSlug": None,
        "sections": [
            {
                "heading": "Why timing is more important than most people think",
                "body": [
                    "Gifting has three components — the gift itself, the moment of delivery, and the context around it (packaging, note, who's there). Most people optimise the first and under-invest in the second and third. Timing is the lever that connects all three.",
                    "In India specifically, timing is harder than in many other markets because courier lead times vary wildly by city, festival season creates capacity spikes, and WhatsApp + UPI create an expectation of instant delivery that doesn't match how physical gifting actually works.",
                    "The practical rule: pick the occasion, pick the gift, then work backward from the occasion date using the windows in this guide. If the category doesn't fit the timing, swap the category rather than hoping the courier comes through.",
                ],
            },
            {
                "heading": "Digital gift cards and experience vouchers — instant",
                "body": [
                    "Digital gift cards (Amazon, Flipkart, Swiggy, Zomato, Myntra, Nykaa) deliver within minutes via email or a code you can share on any channel. Same for BookMyShow vouchers, subscription gifts (Spotify, Netflix, Kindle), and most experience vouchers.",
                    "This is the only category where you can decide on the gift 30 minutes before the occasion and still be on time. It's also the category where last-minute reputation is easiest to recover — a ₹500 Swiggy card with a thoughtful note at 9pm still lands.",
                    "The caveat: gift cards are easy to over-use. If every gift you send is a gift card, the recipient stops noticing. Save the digital-instant category for genuinely last-minute moments, or pair a digital card with a physical item so the digital isn't carrying the whole gift.",
                ],
            },
            {
                "heading": "Same-day delivery — flowers, cakes, chocolates",
                "body": [
                    "Same-day delivery in India is reliable in Pune, Mumbai, Bangalore, Delhi NCR, Hyderabad, and Chennai for flowers, cakes, and some curated hampers. Most vendors accept orders placed by 11am–1pm for same-day delivery later that day.",
                    "Tier-2 and tier-3 cities drop to next-day at best, and during festival season (Diwali week, Valentine's Day, Rakhi) even tier-1 cities can slip. For these peaks, aim to book 2–3 days ahead.",
                    "The practical rule for same-day: decide by 10am, book by noon, and pick a local florist or bakery over a marketplace aggregator. Aggregators add a layer of routing that increases failure rate, especially in peak season.",
                ],
            },
            {
                "heading": "Personalised items — 3 to 7 days",
                "body": [
                    "Custom keepsakes (mugs, photo frames, personalised jewellery, engraved wallets, photo books) need real production time. 3 days is the floor for simple items (a printed mug); 7 days is the floor for premium items (engraved jewellery, larger photo books).",
                    "The common mistake is assuming 'personalised' means 'instant' because the interface feels like a gift card. It doesn't — there's a physical production step. Order a week ahead for anything involving engraving, printing, or custom production.",
                    "If you're inside the 3-day window and still want personalisation, switch to a digital photo book, an audio message, or a custom video — these skip physical production but keep the personalised dimension.",
                ],
            },
            {
                "heading": "Premium items — 2 to 5 days",
                "body": [
                    "Tech, leather goods, fashion, premium skincare hampers, and specialty food usually ship in 2–5 days from major Indian cities. The delivery speed depends more on the vendor's own warehouse location than on the courier.",
                    "A practical approach: when you're 5+ days out from the occasion, you have the full premium category open. When you're 2–3 days out, stick to same-day + digital. Don't gamble on 2-day shipping hitting a 2-day deadline — there's no buffer.",
                    "Festival weeks (Diwali, Valentine's, Rakhi) add 1–3 days across the board. For anything in those windows, order 7+ days ahead.",
                ],
            },
            {
                "heading": "Long-distance and international — 7+ days",
                "body": [
                    "Gifts going to smaller Indian cities, villages, or abroad need 7+ days. For intra-India, most categories are fine with 7 days; for international, allow 10–14 days plus customs risk.",
                    "The trap is forgetting that 'sent from Mumbai, delivered in Mumbai' is different from 'sent from Mumbai, delivered in a tier-3 town in UP'. Many marketplace vendors advertise pan-India shipping but rely on their couriers to handle the last mile, which can fail quietly.",
                    "For long-distance, lean toward categories that don't depend on freshness (gift cards, books, skincare, leather) and avoid time-sensitive items (flowers, cakes, chocolates in summer).",
                ],
            },
            {
                "heading": "Occasion-specific timing — the quick reference",
                "body": [
                    "Birthdays: 5 days before is the ideal send-by date for non-digital gifts. Digital can wait until the day.",
                    "Anniversaries: 5–7 days before. Milestone anniversaries (first, fifth, tenth) often justify booking experiences or travel 2–4 weeks ahead.",
                    "Diwali, Rakhi, Karva Chauth, Bhai Dooj: 7–10 days before. Festival season congestion makes this non-negotiable.",
                    "Valentine's Day: 5–7 days before for physical gifts. Demand spikes on Feb 12–13 and capacity slots fill fast.",
                    "Mother's Day, Father's Day: 5–7 days before. Restaurants and spas fill up even faster than physical gifts for these.",
                    "Weddings: 1 week for non-arrival gifts; for arrival gifts brought in person, no shipping worry.",
                    "Housewarming: 3–5 days before or same-day if you're attending.",
                ],
            },
            {
                "heading": "When you miss the window — how to recover",
                "body": [
                    "Missing the delivery window doesn't have to mean a bad gift. The recovery play: switch to a digital gift card or experience voucher (instant), pair it with a short handwritten note sent via a physical card, and plan a follow-up in-person moment (dinner, a visit) within the next 1–2 weeks.",
                    "The worst recovery is a generic 'sorry I'm late' gift sent late. The best is an honest acknowledgement plus a thoughtful replacement plus a plan to do the thing you should have done originally.",
                    "Timing recovery is easier for birthdays and anniversaries than for festivals — festivals have a fixed moment, birthdays and anniversaries can shift.",
                ],
            },
        ],
        "faqs": [
            {
                "question": "How many days before an occasion should I send a gift in India?",
                "answer": "For non-digital gifts, 5 days before is a safe default. Personalised items need 5–7 days. Festival weeks (Diwali, Rakhi, Valentine's) need 7–10 days. Digital gift cards and experience vouchers can be sent the same day. Long-distance or tier-3 city delivery needs 7+ days.",
            },
            {
                "question": "Can I send a same-day gift in India?",
                "answer": "Yes, for flowers, cakes, chocolates, and curated hampers in major Indian cities (Pune, Mumbai, Bangalore, Delhi, Hyderabad, Chennai) — if booked by noon. Digital gift cards and experience vouchers deliver instantly to any city. Tier-2/3 cities drop to next-day at best.",
            },
            {
                "question": "How much ahead should I order a personalised gift?",
                "answer": "Minimum 3 days for simple personalised items (printed mugs, custom T-shirts). 5–7 days for engraved jewellery, photo books, and custom framed items. During festival weeks, add 2–3 days to every estimate.",
            },
            {
                "question": "What do I do if I missed the delivery window?",
                "answer": "Switch to a digital gift card or experience voucher (instant delivery) paired with a handwritten note. Plan a follow-up in-person moment — dinner, a visit, a call — within 1–2 weeks. Honest acknowledgement plus a thoughtful replacement beats a generic 'sorry I'm late' gift sent late.",
            },
            {
                "question": "Why do Indian couriers slow down during festival season?",
                "answer": "Festival weeks (Diwali, Rakhi, Valentine's Day, Raksha Bandhan) trigger demand spikes across all gifting categories. Courier capacity is fixed, so queues build. Orders that would take 2 days in March may take 4–5 days in the week before Diwali. Plan 7+ days ahead for festival gifting.",
            },
        ],
        "relatedGuideSlugs": ["last-minute-gifts", "gift-cards", "gift-ideas-under-1000"],
        "relatedPostSlugs": [],
        "seoKeywords": [
            "when to send gift before occasion",
            "gift delivery timing india",
            "how many days before birthday to send gift",
            "same day gift delivery india",
            "festival gift delivery timing",
        ],
    }
]


def main() -> None:
    if not all(
        [
            settings.r2_endpoint,
            settings.r2_bucket,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
            settings.r2_public_base_url,
        ]
    ):
        raise SystemExit("R2 is not configured in this environment — aborting.")

    client = _r2_client()
    for post in POSTS:
        record = dict(post)
        record["path"] = f"/blog/{post['slug']}"
        _put_json(client, _post_key(post["slug"]), record)
        print(f"wrote {_post_key(post['slug'])}")

    posts = _rebuild_index(client)
    print(f"rebuilt blog/index.json with {len(posts)} post(s)")
    print(json.dumps([p["slug"] for p in posts], indent=2))


if __name__ == "__main__":
    main()
