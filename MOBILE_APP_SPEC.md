# 📱 Wishkr Mobile App Specification & Product Roadmap

> **Living Document:** This file is the single source of truth for the Wishkr Mobile App (iOS / Android), our hybrid mobile + WhatsApp architecture, design principles, and feature backlog.

---

## 1. Executive Summary & Strategic Pivot

### Why Move to a Mobile App?
Currently, Wishkr operates as a conversational agent inside WhatsApp. While WhatsApp provides zero-friction onboarding, relying solely on WhatsApp chat creates two major bottlenecks:
1. **High LLM Token Consumption:** Simple actions (browsing a list, editing a date, viewing 10 gift ideas) require multi-turn LLM reasoning, driving high API costs.
2. **UX Limitations of Text Chat:** Users cannot visually browse products, view clean calendar grids, or manage wishlists seamlessly via text commands alone.

### The Hybrid Architecture (Mobile App + WhatsApp)
* **Mobile App (The Primary Hub):** Rich, visual, fast, zero-token interactions for browsing, calendar sync, wishlist boards, gift curation, and preference setting.
* **WhatsApp (The High-Open Delivery Network):** Automated occasion reminders, outbound friend invitations, and frictionless fallback for friends who haven't installed the app yet.

```
       ┌────────────────────────────────────────────────────────┐
       │                  WISHKR BACKEND                        │
       │   FastAPI + PostgreSQL + Celery/Scheduler + Gemini    │
       └──────────────┬──────────────────────────┬──────────────┘
                      │ REST API                 │ Meta Cloud API
                      ▼                          ▼
       ┌────────────────────────┐      ┌────────────────────────┐
       │   WISHKR MOBILE APP    │      │   WHATSAPP INTERFACE   │
       │   (iOS / Android)      │      │                        │
       │ • Visual Occasion Hub  │      │ • High-open reminders  │
       │ • Google Calendar Sync │      │ • Invites to friends   │
       │ • Visual Wishlists     │      │ • Quick chat fallback  │
       │ • 1-Tap Reservations   │      │ • Web link previews    │
       │ • In-App Notifications │      └────────────────────────┘
       └────────────────────────┘
```

---

## 2. Core Feature Specifications

### 🎯 Feature 1: Authentication & Seamless Account Link
* **Goal:** Zero-friction login with phone number matching the user's existing WhatsApp profile.
* **Capabilities:**
  * **Phone OTP / WhatsApp Login:** Enter mobile number $\rightarrow$ receive 6-digit OTP (via SMS or instant WhatsApp message).
  * **Legacy Profile Hydration:** Instantly links with all existing occasions, contacts, and wishlist items previously saved via WhatsApp.
  * **Optional Google Sign-In:** Links Google Account for Google Calendar sync.

---

### 📅 Feature 2: Google Calendar Event Sync
* **Goal:** Automatically import birthdays, anniversaries, and personal recurring events so the user never has to enter dates manually.
* **Capabilities:**
  * **One-Tap OAuth Sync:** Connect Google account with `calendar.events.readonly` scope.
  * **Smart Filtering:** Detects all recurring annual events (birthdays, anniversaries, special milestones) and filters out noisy work meetings.
  * **Contact Association:** Matches imported events with existing device contacts or Wishkr contacts by name/email/phone.
  * **Two-Way Status:** Shows sync status, last synced timestamp, and enables 1-tap re-sync.
  * **Custom Inclusion/Exclusion:** Users can review detected events and toggle which ones to add to their Wishkr calendar.

---

### 🎂 Feature 3: Visual Occasion & Calendar Hub
* **Goal:** A clean, intuitive dashboard of all upcoming celebrations.
* **Capabilities:**
  * **Chronological Timeline & Month View:** Visual countdown cards ("Rahul's Birthday in 4 days", "Parents' Anniversary in 18 days").
  * **Quick Add:** Add any custom event in under 10 seconds (Name, Occasion Type, Date, Relationship tag).
  * **Direct Actions:**
    * 💤 **Snooze:** Snooze notifications by 1, 2, or 3 days with a single tap.
    * ⏭️ **Skip This Year:** Skip reminding for this cycle without deleting the person.
    * ✏️ **Edit / Delete / Merge:** Manage duplicate entries visually.
  * **Monthly Spend / Gift Budget Planner:** Optional tracker for planned vs. actual gift spend.

---

### 🔔 Feature 4: Hybrid Notification System (In-App + WhatsApp)
* **Goal:** Ensure alerts are seen without becoming annoying.
* **Capabilities:**
  * **In-App Notification Center:**
    * Badge counter for pending requests, friend reminders, and gift reservations.
    * Real-time notifications: *"Aarav asked for your gift preferences for his Birthday!"*
  * **WhatsApp Outbound Alerts:**
    * Users still receive scheduled WhatsApp reminders (7 days, 3 days, day-of).
    * Reminders include a deep link: `wishkr.app/o/{occasion_id}` to open the event directly in the mobile app.
  * **Push Notifications:** Standard OS push notifications (APNs / FCM) for users with the app installed.

---

### 🎁 Feature 5: Recipient Preference Exchange Hub
* **Goal:** Eliminate the awkwardness of asking friends what they want for their celebration.
* **Capabilities:**
  * **In-App Preference Manager:**
    * Celebrant can select favorite vibe tags (*Coffee, Gadgets, Books, Fitness, Skincare, Sneakerhead*).
    * Attach preferred budget ranges and link specific wishlist items.
  * **Requesting a Friend's Preferences:**
    * User taps *"Ask what they want"* on an upcoming occasion card.
    * If the friend has the app $\rightarrow$ **In-App notification banner + Push**.
    * If the friend does NOT have the app $\rightarrow$ **WhatsApp template message sent automatically** with a lightweight mobile web link or reply prompt.
  * **Preference Notification Flow:**
    * When recipient sets their preferences, the gift-giver gets notified: *"Priya just shared her birthday preferences! Tap to view."*

---

### 📝 Feature 6: Universal Visual Wishlist & Share Extension
* **Goal:** Save products from any store on the internet into one unified list.
* **Capabilities:**
  * **OS Share Sheet Integration:** Share directly from Amazon, Flipkart, Myntra, Nykaa, Safari, or Chrome directly into Wishkr without opening the app.
  * **Link Scraper Engine:** Automatically extracts product image, title, price, and store domain.
  * **Categorization & Privacy Control:**
    * `Public / Friends`: Visible to friends in your circle.
    * `Private`: Only visible to you.
  * **In-App Wishlist Grid:** Clean visual Pinterest-style or card grid with direct store links.

---

### 🤝 Feature 7: Shared Wishlist & Anti-Duplicate Group Gifting
* **Goal:** Allow friend groups to buy wishlist items without duplicate gifts and without spoiling the surprise.
* **Capabilities:**
  * **Celebrant Circle:** Friends can view the celebrant's active shared wishlist.
  * **🔒 Granular Occasion Privacy & Allowlist Access Control (Selective Sharing):**
    * **Per-Occasion Allowlist:** Celebrants can designate specific people (e.g. spouse, parents, 3 best friends) authorized to view the wishlist for that celebration.
    * **Strict Access Control (403 Forbidden):** If Person B (or any unauthorized contact) attempts to inspect the celebrant's wishlist or opens a shared deep link (`wishkr.app/o/{occ_id}`), access is blocked (`HTTP 403 Forbidden: {"error": "access_denied", "message": "This celebration wishlist is private to invited circle members."}`).
    * **Dynamic Member Management:** Celebrants can add or revoke viewing permissions at any time from the celebration settings screen.
  * **48-Hour Reservation Hold:**
    * Tap *"I'm getting this"* to lock the item for 48 hours.
    * Status updates in real-time across all friends: `Available` $\rightarrow$ `Reserved` $\rightarrow$ `Purchased`.
    * Other friends cannot claim it while reserved.
  * **Surprise Safe:** The celebrant *never* sees who reserved or purchased the item.
  * **Purchase Confirmation:** Mark as bought with 1 tap.

---

### 💡 Feature 8: AI Gift Advisor (Visual Discovery Feed)
* **Goal:** Instant, hyper-personalized gift recommendations without waiting for long chat responses.
* **Capabilities:**
  * **Filter Chips:** Filter by Vibe, Relationship (Friend, Spouse, Sibling, Colleague), and Budget sliders (₹500 - ₹10,000+).
  * **Curated Recommendation Cards:** High-quality product images, estimated price, store logo, and a *"Why this fits"* badge.
  * **Zero Hallucination:** Connected to verified catalog and affiliate/marketplace partners.

---

### 🧠 Feature 9: "Log My Gift" & Gifting History
* **Goal:** Complete memory of past gifts so you never repeat a gift.
* **Capabilities:**
  * **Timeline per Contact:** Tap on any contact to view their gifting timeline (*"2025: Fossil Watch", "2024: Nike Sneakers"*).
  * **Quick Log:** Tap *"Log gift given"* after an occasion passes.
  * **Anti-Duplicate Warning:** If you attempt to buy or reserve something similar to a past gift, the app gently alerts you.

---

## 3. UI / UX Design Guidelines

> **Guiding Principle: "Calm, Crisp, Minimal Interaction"**  
> Gifting should bring joy, not cognitive load. Avoid crowded dashboards, aggressive banners, or complicated multi-step forms.

1. **Information Hierarchy:**
   * **Home Tab:** Immediate focus on *"Next Up"* (the nearest 1–3 occasions) with instant action buttons.
   * **Occasions Tab:** Calendar / list view with filter tags (Family, Close Friends, Colleagues).
   * **Wishlist Tab:** Personal wishlist + Friends' wishlists.
   * **Discover Tab:** AI gift inspiration feed.
2. **Minimal Interaction:**
   * Primary actions (Snooze, Reserve, Add Link) must require **no more than 1–2 taps**.
   * Forms should auto-populate from phone contacts or calendar metadata whenever possible.
3. **Aesthetics:**
   * Modern neutral theme with warm accent tones (warm coral, soft gold, or deep indigo).
   * High readability typography (Inter / SF Pro / Outfit).
   * Smooth micro-interactions for state transitions (e.g., claiming a gift, snoozing a reminder).

---

## 4. Backend API Endpoints (Mobile Contract)

The existing FastAPI backend will expose standard RESTful endpoints for the mobile client:

| Domain | Method | Endpoint | Description |
| :--- | :--- | :--- | :--- |
| **Auth** | `POST` | `/api/v1/auth/request-otp` | Request OTP via SMS or WhatsApp |
| | `POST` | `/api/v1/auth/verify-otp` | Verify OTP and return JWT session |
| **Calendar**| `GET` | `/api/v1/calendar/google/auth-url` | Generate Google OAuth2 consent URL |
| | `POST` | `/api/v1/calendar/google/sync` | Sync events from Google Calendar |
| **Occasions**| `GET` | `/api/v1/occasions` | List user's occasions (sorted by days until celebration) |
| | `POST` | `/api/v1/occasions` | Create a new occasion & contact (supports `is_selective_sharing`) |
| | `PUT` | `/api/v1/occasions/{id}` | Update occasion details |
| | `DELETE`| `/api/v1/occasions/{id}` | Soft-delete an occasion |
| | `POST` | `/api/v1/occasions/{id}/snooze` | Snooze reminders for $N$ days |
| | `POST` | `/api/v1/occasions/{id}/skip` | Skip reminding for current year |
| | `GET` | `/api/v1/occasions/{id}/access` | Get allowed circle contacts for this occasion |
| | `PUT` | `/api/v1/occasions/{id}/access` | Update selective access list (phone/contact allowlist) |
| **Wishlist**| `GET` | `/api/v1/wishlist` | Get user's personal wishlist |
| | `POST` | `/api/v1/wishlist/link` | Add URL (auto-scrapes metadata: title, price, image) |
| | `DELETE`| `/api/v1/wishlist/{item_id}`| Remove item from wishlist |
| **Shared** | `GET` | `/api/v1/occasions/{id}/shared-wishlist` | View celebrant's shared wishlist (Enforces allowlist: 403 if unauthorized) |
| | `GET` | `/api/v1/friends/wishlist/{occ_id}` | Alias for friend wishlist viewing with allowlist check |
| | `POST` | `/api/v1/friends/wishlist/reserve` | 48-hour atomic reservation |
| | `POST` | `/api/v1/friends/wishlist/confirm` | Confirm purchase of reserved item |
| **Prefs** | `GET` | `/api/v1/preferences` | Get user's gifting preferences |
| | `PUT` | `/api/v1/preferences` | Update own gifting preferences |
| | `POST` | `/api/v1/preferences/request`| Trigger preference request to contact |
| **Advisor** | `POST` | `/api/v1/recommendations/discover` | Query curated AI gift ideas |

---

## 5. Development & Rollout Phases

- [x] **Phase 0: Architecture & Spec Alignment** (Completed)
  - Finalize hybrid mobile + WhatsApp flow.
  - Define data schema extensions for tokens and REST contracts.
- [x] **Phase 1: REST API Foundation & Google Calendar Sync** (Completed - 100%)
  - [x] Mobile auth routes (OTP via WhatsApp/SMS + JWT Bearer dependency).
  - [x] Occasion CRUD REST routes (`GET /api/v1/occasions`, `POST`, `DELETE`).
  - [x] Occasion access list endpoints (`GET/PUT /api/v1/occasions/{id}/access`).
  - [x] Occasion reminder lifecycle routes (`POST /api/v1/occasions/{id}/snooze`, `skip`).
  - [x] Personal Wishlist REST endpoints (`GET /api/v1/wishlist`, `POST /api/v1/wishlist/link`, `DELETE /api/v1/wishlist/{id}`).
  - [x] Shared Wishlist endpoint with allowlist enforcement (`GET /api/v1/occasions/{id}/shared-wishlist`).
  - [x] Preference REST endpoints (`GET/PUT /api/v1/preferences`, `POST /api/v1/preferences/request`).
  - [x] Google Calendar OAuth2 consent URL & sync background worker (`GET /api/v1/calendar/google/auth-url`, `POST /api/v1/calendar/google/sync`).
  - [x] Push notification device token registration (`POST /api/v1/notifications/device-token`, `GET /api/v1/notifications`).
- [x] **Phase 2: Mobile App MVP Core** (Completed - 100%)
  - [x] Foundation: Riverpod 3 architecture, GoRouter guards, Drift offline SQLite, DioClient interceptor.
  - [x] Auth flow: Phone input, OTP entry, auto-token storage in SecureStorageService.
  - [x] Occasion hub: Upcoming celebration cards, countdown logic, Add Occasion sheet modal.
  - [x] Per-Occasion Privacy & Allowlist Selector: Selective Wishlist Sharing toggle & allowed contacts in AddOccasionSheet with backend sync.
  - [x] Personal Wishlist Management: Interactive wishlist view with link paste, price in ₹, manual entry, Drift SQLite store, and backend sync.
  - [x] Discover screen & In-App Notification Center UI.
  - [x] **Remaining Phase 2 items:**
    1. **OS Share Sheet Extension:** Ingest URLs shared from Amazon, Flipkart, Myntra, or browsers directly into Wishkr personal wishlist (`ACTION_SEND` on Android, `ShareIntentService`).
    2. **Push Notifications Setup:** FCM / APNs registration service (`NotificationService`) and token exchange with backend.
    3. **Google Calendar Sync Screen:** Circle screen header action to trigger Google Calendar sync and refresh celebrations cache.
- [x] **Phase 3: Social & Shared Gifting Flows** (Completed - 100%)
  - [x] Shared wishlist viewing with live 48-hr item reservations and anti-spoiler status (`SharedWishlistScreen`, `POST /api/v1/friends/wishlist/reserve`, `/confirm`, `/cancel`).
  - [x] Enforced allowlist privacy verification on shared wishlists (403 for unauthorized users & `_PrivateCelebrationState` lock screen).
  - [x] In-app + WhatsApp fallback preference exchange requests ("Ask what they want" action button on CelebrationCard + `POST /api/v1/preferences/request`).
  - [x] Universal deep linking support (`/occasions/:occasionId/shared-wishlist` and `/o/:occasionId`).
- [x] **Phase 4: AI Visual Discovery & Polish** (Completed - 100%)
  - [x] Dynamic AI gift inspiration feed connected to live product catalog (`POST /api/v1/recommendations/discover`, `DiscoverScreen`).
  - [x] Interactive AI Gift Advisor modal with recipient relationship tuning (Friend, Partner, Sibling, Mom, Dad, Colleague).
  - [x] "Log My Gift" history timeline per contact (`ContactGiftingTimelineSheet`, `GET/POST /api/v1/contacts/{contact_id}/gifts`).
  - [x] Anti-duplicate validation checks (`POST /api/v1/recommendations/check-duplicate` and anti-duplicate check dialogs).
  - [x] 1-Tap Save to Wishlist from Discovery feed directly persisted to SQLite and synced to server.
  - [x] Micro-animations and haptic feedback (`HapticFeedback.lightImpact()`, `HapticFeedback.mediumImpact()`).
  - [x] End-to-end integration and widget test coverage (6 backend integration tests, 24 mobile Flutter tests).
