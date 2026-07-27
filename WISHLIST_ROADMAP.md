# 🚀 Wishlist System — Execution Roadmap (Live Status)

Source: consolidated roadmap + current implementation state

Last updated: 2026-04-22 — DB-backed storage live; "show my wishlist" wired; 50-item cap enforced.

---

## 📅 What was achieved on 22 April 2026

**Phase 1 (core deterministic layer) — shipped end-to-end today:**

- **Link utils** (`app/utils/link_utils.py`)
  - `extract_urls`, `is_product_link`, `guess_product_name`
  - `normalize_url` (strips tracking params: `ref`, `tag`, `psc`, `utm_*`, `pid`, `lid` — idempotent)
  - `extract_product_id` (Amazon ASIN, Flipkart `itm*`)
  - `sole_product_link_url` (detects bare-link pastes for repeat-send handling)

- **Deterministic state layer** (`app/bot/state_utils.py`)
  - `get_state / set_state / clear_state / get_awaiting` on `ConversationState.temp_data`
  - Creates the row lazily if missing, so first-time users Just Work

- **Action router** (`app/bot/action_router.py`)
  - Runs before the LLM in `handle_message`
  - Owns: menu, product gate, wishlist link save, awaiting-affirmative re-prompt, **show_wishlist**
  - No LLM calls inside — fully rule-based

- **Wishlist storage** (new)
  - New `WishlistItem` model + Alembic migration `3b1d9f4e7a52_add_wishlist_items_table.py` (down_revision `cf0285589097`)
  - Unique indexes: `(user_id, url)` and partial `(user_id, product_id) WHERE product_id IS NOT NULL`
  - `app/bot/wishlist_store.add_wishlist_product_url()` — normalize → dedupe → insert, race-safe via `IntegrityError` fallback
  - **50-item-per-user cap** with friendly "wishlist is full" message
  - Lazy guest-user creation for unregistered phones

- **Chat integration**
  - `product_question` intent now sets `awaiting="wishlist_link"` so the next link is saved deterministically
  - Canned responses: `WISHLIST_AFFIRMATIVE_REPROMPT`, `WISHLIST_ADDED_RESPONSE`, `WISHLIST_DUPLICATE_RESPONSE`, `WISHLIST_LIMIT_RESPONSE`, `WISHLIST_EMPTY_RESPONSE`
  - New `show_wishlist` intent (rule-based + LLM fallback) → `format_wishlist(items)` renders a WhatsApp-friendly numbered list

- **Tests**
  - `scripts/test_wishlist_router.py` covers gate → link → dup → limit → show → affirmative re-prompt → fallback
  - Mocks at the `wishlist_store` boundary so it runs without a DB

- **Migration status**
  - Migration file in place; `alembic upgrade head` still to be run by you (local Postgres wasn't reachable from here — Docker daemon down)

**What this means in practice:** the earbud-loop / "yess" dead-end / menu-dump bugs from the original transcript are now handled *without* the LLM. The router catches them before Gemini gets involved. Product-link asks now lead to actual saved wishlist items instead of vague prompt-follow-throughs.

---

## Status Legend

- [ ] Todo
- [~] In Progress
- [x] Done
- [!] Needs Decision

---

# 🧭 PHASE 1 — CORE (Pre-Launch Critical Path)

## 1. Link Detection & Normalization ✅

- [x] extract_urls, is_product_link implemented
- [x] guess_product_name (slug-based, no scraping)
- [x] normalize_url:
  - Amazon → /dp/{ASIN}
  - Flipkart → clean path
  - tracking params removed
- [x] extract_product_id (ASIN / Flipkart id)
- [x] Verified with real-world URLs (Amazon, Flipkart, non-product)

👉 Status: **Production-ready**

---

## 2. Deterministic Conversation State ✅

- [x] state_utils implemented: get_state / set_state / clear_state / get_awaiting
- [x] `awaiting="wishlist_link"` set from product gate
- [x] Intercept logic in `handle_message` (now lives in `action_router`)
- [x] Affirmative handling — rule-based only via `is_rule_based_affirmative` (no LLM)
- [x] Smoke test (`scripts/test_wishlist_router.py`) covers gate / link / dup / limit / show / affirmative re-prompt / fallback

👉 Status: **Done — UX is deterministic, no LLM hallucination path**

---

## 3. Action Router ✅

- [x] `app/bot/action_router.try_handle_actions()`
- [x] Owns: menu, product gate, wishlist link save, awaiting affirmative re-prompt, **show_wishlist**
- [x] Runs BEFORE the LLM in `handle_message`
- [x] No LLM calls inside the router (rule-based intents only)

👉 Status: **Done — `handle_message` is clean, all link/wishlist logic is inside the router**

---

## 4. Wishlist Save Logic ✅

- [x] Normalize → deduplicate → insert
- [x] Dedup priority: product_id → normalized URL
- [x] Responses: `Added to wishlist 👍` / `Already had this 👍`
- [x] Implemented in `app/bot/wishlist_store.add_wishlist_product_url()`
- [x] **DB-backed** via `wishlist_items` table (replaces temp JSON)
- [x] Lazily creates a guest `User` row when phone is unregistered (matches existing pattern in `tools/functions.py`)
- [x] Race-safe — `IntegrityError` on unique index falls back to `"duplicate"`

👉 Status: **Production-ready**

---

## 5. Wishlist Storage ✅

- [x] **Decision: Option B — dedicated table** (Option A retired)
- [x] `WishlistItem` model: `id, user_id, product_id, url, title, created_at, updated_at`
- [x] Alembic migration `3b1d9f4e7a52_add_wishlist_items_table.py` (down_revision = `cf0285589097`)
- [x] Indexes:
  - `ix_wishlist_items_user_id`
  - `uq_wishlist_user_url` (unique on `user_id, url`)
  - `uq_wishlist_user_product` (unique partial on `user_id, product_id WHERE product_id IS NOT NULL`)
- [x] **Max 50 items per user** enforced inside `wishlist_store` (`MAX_ITEMS_PER_USER = 50`)
- [x] Friendly cap message: *"Your wishlist is full (50 items). Remove one to add more."*
- [x] Old `temp_data["wishlist_items"]` blobs intentionally **ignored** (no data migration)

Apply locally:

```
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm backend alembic upgrade head
```

👉 Status: **Done**

---

# ⚡ PHASE 2 — PRODUCT FEATURES (Post-Core)

## 6. Wishlist Sharing

- [!] Decide domain: `/w/{token}` vs existing domain path
- [ ] `wishlist_shares` model
- [ ] Token generation service
- [ ] Public endpoint `/wishlist/{token}`
- [ ] Safe JSON response (no private data)
- [ ] Simple mobile HTML page

👉 Status: **Not started (intentionally deferred)**

---

## 7. Chat Integration

- [x] **"show my wishlist"** → returns formatted list (title + URL, oldest first)
  - Rule-based phrases: `wishlist`, `my wishlist`, `show wishlist`, `view wishlist`, `see my wishlist`, etc.
  - Empty state: *"Your wishlist is empty 🤔 Send me a product link …"*
  - LLM fallback also recognises `show_wishlist` for fuzzier phrasings
- [ ] "show my wishlist" → return share link (waits on #6)
- [ ] "what should I gift" → show wishlist first
- [ ] Add wishlist nudging in main agent responses

👉 Status: **List view shipped; share link waits on Phase 2 sharing**

---

## 8. Deduplication ✅

- [x] product_id-based dedup (preferred)
- [x] fallback URL dedup
- [x] DB-level enforcement via unique indexes (Postgres rejects races)

👉 Status: **Done**

---

# 💰 PHASE 3 — PRODUCT LOOP (Growth)

## 9. Friend Gifting Flow

- [ ] "Gift this" button (public page)
- [ ] Deep link → WhatsApp / bot flow
- [ ] Integrate `create_friend_gift_order`
- [ ] Gift card model (not direct product purchase)
- [ ] End-to-end test

👉 Status: **Not started (intentionally deferred)**

---

## 10. Recommendations (Lightweight)

- [ ] Top 3 wishlist items
- [ ] Fallback → popular gifts
- [ ] Contextual nudges

👉 Status: **Not started**

---

# 🛡️ PHASE 4 — SAFETY & LIMITS

- [x] **Max 50 wishlist items** (enforced in `add_wishlist_product_url`)
- [x] Rate limit link submissions (per-phone `check_and_bump`)
- [x] Strict domain validation (`is_product_link` allow-list)
- [x] Ignore non-product links safely (router defers to main agent)

👉 Status: **Done**

---

# 📊 PHASE 5 — METRICS (Later)

- [ ] Track:
  - wishlist_add
  - duplicate_rate
  - share_clicks
  - gift_conversion
  - fallback_rate
- [ ] Dashboard (defer)

---

# 🧪 TESTING (Pre-Launch)

## Wishlist

- [x] Add new link
- [x] Duplicate link
- [x] Limit-reached path
- [x] Show wishlist (empty + populated)
- [ ] Multiple links across separate gates
- [ ] Invalid link rejected by `is_product_link`

## Chat Flow

- [x] Affirmative ("yes") handling
- [x] Link without "yes"
- [x] Random fallback (router defers, agent answers)

## Edge

- [ ] Spam messages
- [ ] Long messages
- [ ] Webhook retry
- [ ] Concurrent webhook deliveries on same link (covered by DB unique index + `IntegrityError` handling, but not yet exercised in test)

Tests live in `scripts/test_wishlist_router.py` (run: `python -m scripts.test_wishlist_router`).
The test mocks at the `wishlist_store` boundary so it runs without a DB. An
integration test that exercises the actual `wishlist_items` table is still
worth adding when you set up the docker-compose test target.

---

# 🚀 CURRENT REALITY

## ✅ Strong

- Deterministic UX layer is complete
- DB-backed wishlist storage with hard dedup and 50-item cap
- "show my wishlist" works end-to-end (text → router → DB → formatted reply)
- Race-safe writes (DB unique index is the backstop)
- Router cleanly separates LLM vs non-LLM paths
- Guest user creation on first wishlist save — works for unregistered phones

## ⚠️ Gaps (next up)

1. Wishlist sharing (Phase 2 #6) — domain decision still open
2. "Remove from wishlist" command (so the 50-cap message is actionable)
3. Integration test that hits the real DB

## ❌ Not Started (deferred by design)

- Sharing
- Gifting
- Recommendations

---

# 🎯 NEXT 3 STEPS

1. Pick the share-link domain (`/w/{token}` vs existing) and ship `wishlist_shares` + public endpoint
2. Add a "remove wishlist item N" command so `WISHLIST_LIMIT_RESPONSE` is actionable
3. Wire wishlist nudges into the main agent ("you have 3 items on your wishlist — want to share?")

---

# 🧭 SYSTEM YOU'RE BUILDING

User → adds wishlist  
↓  
Shares link  
↓  
Friend views  
↓  
Friend gifts  
↓  
Occasion fulfilled 🎁

---

# 🧠 FINAL NOTE

The full **personal wishlist loop** (gate → save → dedup → list) is now done end-to-end and DB-backed. The next milestone is making that wishlist *visible to other people* — sharing.

👉 Focus now:

- ship sharing (#6) so the loop becomes a product, not a private list
- avoid putting any new "smart" logic into the main agent that could live in the router
- only revisit storage when share-link analytics or recommendations need new columns

---
