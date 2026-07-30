# Wishkr Commerce Order Growth Action Plan

## Document Status

- **Status:** Proposed execution plan
- **Prepared:** 29 July 2026
- **Scope:** Wishkr production catalogue, vendors, pricing, shipping, delivery promises, storefront, checkout, positioning, and commerce analytics
- **Repositories:** `wishkr-backend` and `wishkr-web`
- **Production evidence:** Read-only aggregate analysis; no customer names, phone numbers, addresses, order IDs, credentials, or other personal data are included

## Purpose

Wishkr has a functioning physical-product storefront and checkout, but order
volume is extremely low. This document converts the production analysis into
an executable backlog.

The plan is designed to answer five questions:

1. Are product prices too high?
2. Are delivery charges preventing purchases?
3. Are delivery dates and fulfilment promises unsuitable for gifting?
4. Are vendor readiness and vendor-specific checkout constraints causing
   problems?
5. How should Wishkr position and merchandise itself to generate orders?

## Executive Decision

Wishkr should not compete as another large gifting marketplace. It should
position itself as a gifting concierge:

> **Wishkr remembers every important date, picks three gifts that fit the
> person and budget, and gets one there on time.**

The recommended operating model is:

- Keep the reminder and gift-planning service available nationally.
- Run physical fulfilment Pune-first until vendor reliability and delivery
  speed are proven.
- Promote a small, gift-ready catalogue instead of a large, repetitive
  catalogue.
- Include standard delivery in the displayed price of promoted products.
- Keep delivery dates, but make them accurate, pincode-aware, and aligned with
  reminder timing.
- Allow only operationally ready vendors and products to receive orders.

## Production Baseline

This snapshot was observed on 29 July 2026.

### Catalogue

| Metric | Production value |
| --- | ---: |
| Active products from active vendors | 148 |
| Jewellery products | 102 (69%) |
| Published but unavailable products | 19 |
| Products marked as bestsellers | 12 |
| Out-of-stock bestsellers | 2 |
| Products supporting a gift message | 17 |
| Customisable products | 33 |
| Products with free delivery | 30 |
| Products with ₹59 or lower paid delivery | 106 |
| Products with ₹60–₹100 paid delivery | 12 |
| Products with an approximately six-day promise | 118 |
| Products with a one- or two-day promise | 7 |

### Active category mix

| Category | Active products | Observed concern |
| --- | ---: | --- |
| Jewellery | 102 | Dominates the catalogue; many low-value or duplicate listings |
| Candles | 22 | Some useful products, but several are not viable as standalone gifts |
| Flowers | 9 | Premium-heavy and mostly local |
| Hampers | 8 | Insufficient affordable choice |
| Home Fragrance | 4 | Narrow range |
| Home Décor & Keepsakes | 3 | Narrow range |
| Cakes | 0 | Advertised but unavailable |
| Chocolates | 0 | Advertised but unavailable |
| Personalised/photo gifts | 0 true category inventory | Advertised but unavailable |

### Orders

| Status | Count | Total value |
| --- | ---: | ---: |
| Paid physical-product orders | 2 | ₹459 |
| Pending/unpaid physical-product orders | 2 | ₹11,998 |

The unpaid orders were a ₹1,999 flower order and a ₹9,999 hamper order. Both
had free delivery, so delivery fees did not cause those two specific
late-checkout abandonments. The sample is too small to attribute abandonment
to price, trust, payment, or intent.

The paid orders must be classified as genuine customer orders or internal test
orders before they are used as demand evidence.

### Pricing

The global storefront markup was `0%`, flash pricing was disabled, and the
displayed prices were therefore primarily vendor-supplied prices.

Observed pricing conclusions:

- Jewellery is generally inexpensive, but ₹59 delivery is disproportionate
  to the product value.
- Candles are broadly competitive, but extremely low-priced standalone items
  do not feel gift-ready once delivery is added.
- Flowers are positioned well above the mainstream online flower market.
- Hampers lack affordable entry points and over-emphasise premium products.
- The ₹9,999 Birthday Combo should not lead a general bestseller collection.

### Market benchmark

The benchmark below was checked on 29 July 2026 and should be refreshed before
major pricing decisions.

| Competitor evidence | Implication for Wishkr |
| --- | --- |
| [FNP delivery FAQ](https://www.fnp.com/info/faq) offers a free standard delivery slot, with additional charges for premium delivery windows | Free/included standard delivery is an established customer expectation |
| [FNP flower listings](https://www.fnp.com/instant-delivery-flowers-lp) include many bouquets around ₹349–₹1,499 | Wishkr's ₹1,999 minimum and ₹3,999 median flower prices are premium, not mainstream |
| [FlowerAura rose listings](https://www.floweraura.com/flowers/roses) show products around ₹495–₹1,945 with delivery measured in hours | Wishkr cannot lead with six-day delivery for mainstream flowers |
| [IGP delivery FAQ](https://www.igp.com/faq) states that most domestic standard shipping is free and quotes same-day or two-to-five-day fulfilment for regular pincodes | Wishkr needs an included standard option and explicit pincode-aware dates |
| [FNP birthday hampers](https://www.fnp.com/gift-hampers/birthday-lp) include entry products around ₹399, ₹829, ₹939, and ₹999 | Wishkr needs affordable hampers below its current ₹1,500 entry point |
| [IGP same-day gifts](https://www.igp.com/same-day-delivery-gifts) include cakes, flowers, hampers, and personalised gifts across mainstream budgets | Wishkr should compete through curation and reminders, not catalogue breadth |

### Baseline refresh rule

Production catalogue and order figures change over time. Before approving a
launch, promotion, or vendor pause:

1. Rerun read-only aggregate catalogue, shipping, delivery, stock, vendor, and
   order-status checks.
2. Record the observation date.
3. Compare the result with this baseline.
4. Update this document's figures and change log if the differences materially
   alter a decision.

## Strategic Decisions

### Shipping

**Decision:** Do not remove every delivery charge blindly. Include standard
delivery in the advertised price of promoted products, bundle or remove
unviable low-value items, and charge separately only for premium delivery
services.

Rules:

- Do not sell products below ₹199 as standalone shipped gifts.
- Target delivered-price bands such as ₹299, ₹499, ₹799, ₹999, and ₹1,499.
- Keep shipping below 10–12% of product value where possible.
- Offer free/included standard delivery on the hero catalogue.
- Charge separately for same-day, fixed-time, early-morning, or midnight
  delivery.
- Negotiate vendor shipping at the order level rather than applying
  disproportionate per-product charges.

### Delivery date

**Decision:** Keep delivery dates. Fix delivery speed and promise accuracy.

Rules:

- Use product- and vendor-specific lead times.
- Show `Earliest delivery: DATE` or an accurate date range.
- Never show the contradictory phrase `Delivers by DATE or later`.
- Recommend only products that can arrive before the occasion.
- Schedule reminders at least `delivery lead time + 2 days` before the
  occasion.
- Do not send a five-day reminder for a product that needs six days.

### Vendors

**Decision:** Vendor readiness is a release gate, not an administrative note.

No vendor should receive live orders until KYC, policies, delivery SLA,
serviceability, inventory, notification, and test fulfilment are complete.

### Positioning

**Decision:** Lead with occasion memory, three curated choices, and dependable
arrival—not marketplace size.

Wishkr should initially offer:

- **Safe choice:** broadly suitable and low risk.
- **Personal choice:** matched to the recipient.
- **Premium choice:** higher-value or more elaborate.

Every recommended choice must be in stock, serviceable, and able to arrive
before the occasion.

## Ownership Roles

Specific people can be assigned during planning. This document uses the
following owner roles:

- **ENG-BE:** Backend engineering
- **ENG-WEB:** Web/storefront engineering
- **CAT:** Catalogue and merchandising
- **VOPS:** Vendor operations
- **OPS:** Order and fulfilment operations
- **GROWTH:** Positioning, acquisition, and lifecycle marketing
- **DATA:** Analytics and reporting

## P0: Stop Active Conversion Leaks

Target completion: within 72 hours of starting execution.

| ID | Action | Owner | Status | Completion test |
| --- | --- | --- | --- | --- |
| COM-P0-001 | Remove unavailable products from homepage rails and default shop results | ENG-WEB | Implemented; awaiting deploy | No out-of-stock product appears in a homepage rail or the default shop grid |
| COM-P0-002 | Keep unavailable product pages available only for direct/SEO access with a clear unavailable state | ENG-WEB | Implemented; awaiting deploy | Direct product URL remains valid, but the item cannot be promoted or purchased |
| COM-P0-003 | Remove bestseller status from products without verified sales or an explicit editorial reason | CAT | Not started | Every bestseller has a recorded sales or editorial justification |
| COM-P0-004 | Remove the ₹9,999 Birthday Combo from the general homepage/shop lead positions | CAT | Not started | Product appears only in a premium collection |
| COM-P0-005 | Remove or hide empty Cakes, Chocolates, and Personalized navigation choices | ENG-WEB | Not started | Every visible category opens a non-empty purchasable collection |
| COM-P0-006 | Enforce a single-vendor cart before checkout | ENG-WEB | Not started | Adding a second vendor shows a clear separate-order decision; checkout cannot fail because multiple vendors are present |
| COM-P0-007 | Use the same authoritative delivered total on PDP, drawer, cart, checkout, and Razorpay | ENG-WEB, ENG-BE | Not started | The amount displayed at every stage exactly matches the amount submitted for payment |
| COM-P0-008 | Replace `Delivers by DATE or later` with accurate earliest-date or date-range copy | ENG-WEB | Not started | No contradictory delivery phrase remains |
| COM-P0-009 | Allow Add to Cart before pincode confirmation | ENG-WEB | Not started | A buyer can build a cart without knowing the recipient pincode; payment remains blocked until serviceability is confirmed |
| COM-P0-010 | Store and reuse one pincode across product pages and checkout | ENG-WEB | Not started | A buyer enters the pincode only once per browsing session unless they choose to change it |
| COM-P0-011 | Classify the four existing product orders as customer or internal/test | OPS | Not started | Each order has an internal classification without exposing customer data |
| COM-P0-012 | Complete one production test order per active vendor | OPS, VOPS | Not started | Payment, notification, acknowledgement, fulfilment update, tracking, and completion are verified |
| COM-P0-013 | Fix the `/vendor-app` canonical redirect expectation or update the SEO audit to match the intended route | ENG-WEB | Not started | Production SEO audit passes with no unexplained route failure |

### Engineering locations

Relevant implementation areas:

- Backend single-vendor and shipping logic:
  `app/api/product_orders.py`
- Backend delivery fallback:
  `app/services/delivery_estimate.py`
- Web cart state and shipping calculation:
  `wishkr-web/src/app/lib/cart.tsx`
- Product page pincode and delivery UI:
  `wishkr-web/src/app/shop/[facet]/ProductDetailClient.tsx`
- Checkout quote and payment flow:
  `wishkr-web/src/app/checkout/CheckoutClient.tsx`
- Homepage product rails:
  `wishkr-web/src/app/components/GiftingFrontDoor.tsx`
- Storefront category navigation:
  `wishkr-web/src/app/components/Navbar.tsx`

## P0: Vendor Readiness Gate

Target completion: before the vendor receives another promoted order.

### Required go-live fields

- [ ] Onboarding status is `live`
- [ ] KYC is complete
- [ ] Bank/payout readiness is confirmed
- [ ] Delivery arrangement is confirmed
- [ ] Delivery lead time is configured
- [ ] Same-day cutoff is configured when applicable
- [ ] Serviceable cities/pincodes are verified
- [ ] Delivery policy is present
- [ ] Return policy is present
- [ ] Cancellation policy is present
- [ ] Shipping rule is confirmed
- [ ] Inventory update process is working
- [ ] Vendor notification is working
- [ ] Vendor acknowledgement is working
- [ ] One test order has completed successfully

### Vendor-specific actions

| ID | Vendor | Action | Owner | Status | Completion test |
| --- | --- | --- | --- | --- | --- |
| VEN-P0-001 | PyariSakhee | Configure delivery SLA and delivery policy; reconcile 19 unavailable products; validate ₹59 shipping | VOPS, CAT | Not started | Only available inventory is promoted and SLA/policy are complete |
| VEN-P0-002 | Pratiksha Decore | Add delivery, return, and cancellation policies; configure SLA | VOPS | Not started | All vendor readiness fields and a test order pass |
| VEN-P0-003 | house.ofzahraa | Resolve `prospect` status; complete onboarding, policies, serviceability, and SLA | VOPS | Not started | Status is `live` and no readiness field is missing |
| VEN-P0-004 | Garvya | Configure delivery SLA and renegotiate/test ₹100 shipping | VOPS | Not started | Delivery promise is explicit and standard shipping meets the commercial rule |
| VEN-P0-005 | Kala Avishkar | Add category, delivery policy, serviceability, and SLA | VOPS, CAT | Not started | Vendor is completely classified and testable |
| VEN-P0-006 | Little. Whittle | Complete KYC and reconcile vendor-level versus product-level lead times | VOPS | Not started | KYC is complete and all displayed dates match fulfilment capability |
| VEN-P0-007 | Words But More | Deactivate storefront/vendor availability until products are purchasable | VOPS | Not started | Vendor is hidden or has verified live inventory |

## P1: Catalogue Reset

Target completion: within two weeks.

### Target promoted catalogue

Reduce the promoted catalogue to approximately 30–40 strong, gift-ready
products.

| Delivered-price band | Target count | Role |
| --- | ---: | --- |
| ₹299–₹499 | 8–10 | Small, thoughtful gifts |
| ₹500–₹999 | 10–12 | Primary gifting range |
| ₹1,000–₹1,999 | 8–10 | Flowers, hampers, personalised products |
| ₹2,000–₹4,999 | 4–6 | Premium gifting |
| Above ₹5,000 | 2–3 maximum | Dedicated luxury collection |

### Catalogue actions

| ID | Action | Owner | Status | Completion test |
| --- | --- | --- | --- | --- |
| CAT-P1-001 | Remove or bundle standalone products below ₹199 | CAT, VOPS | Not started | No promoted shipped product below ₹199 remains unbundled |
| CAT-P1-002 | Merge duplicate product names and near-identical listings | CAT | Not started | Duplicate-name report is empty or each duplicate is a meaningful variant |
| CAT-P1-003 | Correct spelling, naming, capitalization, and category errors | CAT | Not started | Editorial review passes for every promoted product |
| CAT-P1-004 | Require at least three clear product images for hero products | CAT, VOPS | Not started | Every hero PDP has at least three usable images |
| CAT-P1-005 | Add `What's included` to every promoted product | CAT, VOPS | Not started | Field is non-empty and product-specific |
| CAT-P1-006 | Add packaging/gift-wrapping information | CAT, VOPS | Not started | PDP explains how the gift arrives |
| CAT-P1-007 | Enable gift messages wherever operationally possible | CAT, VOPS | Not started | All suitable products support a message or explain why not |
| CAT-P1-008 | Tag every product by recipient, occasion, budget, urgency, and personalisation level | CAT | Not started | Products can support concierge-style shortlists |
| CAT-P1-009 | Treat inventory of one as `Last piece` rather than normal evergreen inventory | ENG-WEB, CAT | Not started | Scarce inventory is explicit and cannot dominate collections |
| CAT-P1-010 | Create delivered-price collections: Under ₹499, Under ₹999, ₹1,000–₹1,999, and Premium | CAT, ENG-WEB | Not started | Collections contain only in-stock, serviceable products |
| CAT-P1-011 | Create a `Same-day/Next-day Pune` collection | CAT, VOPS, ENG-WEB | Not started | At least 10 products have verified one- or two-day fulfilment |
| CAT-P1-012 | Add at least 6–10 purchasable cakes | VOPS, CAT | Not started | Cake navigation can be restored without an empty result |
| CAT-P1-013 | Add at least 6–10 chocolate/sweets products | VOPS, CAT | Not started | Chocolate navigation can be restored without an empty result |
| CAT-P1-014 | Add at least 6–10 genuine personalised/photo products | VOPS, CAT | Not started | Personalised collection contains real customisable products |
| CAT-P1-015 | Add at least 8 affordable hampers | VOPS, CAT | Not started | Hamper range includes delivered options below ₹1,500 |
| CAT-P1-016 | Add at least 5 plants/home-gift options | VOPS, CAT | Not started | Neutral gifting has a viable collection |
| CAT-P1-017 | Add at least 8 same-day/next-day Pune flower products | VOPS, CAT | Not started | Flower range includes mainstream prices and verified fast delivery |
| CAT-P1-018 | Configure bouquet and hamper recipes, size options, and seasonal prices using the [bouquet rollout plan](BOUQUET_HAMPER_DYNAMIC_PRICING.md) | VOPS, CAT, ENG | Implemented locally; awaiting production setup | Pilot products expose verified size options without revealing vendor costs, and checkout charges the active database price |

## P1: Shipping and Delivered Pricing

Target completion: within two weeks.

| ID | Action | Owner | Status | Completion test |
| --- | --- | --- | --- | --- |
| SHIP-P1-001 | Calculate unit economics for every promoted product | CAT, VOPS, DATA | Not started | Product price, vendor payout, shipping, payment fee, support allowance, and Wishkr margin are recorded |
| SHIP-P1-002 | Negotiate vendor-level order shipping for hero products | VOPS | Not started | Standard shipping is commercially viable and not disproportionate |
| SHIP-P1-003 | Include standard delivery in advertised hero-product prices | CAT, ENG-WEB | Not started | Hero PDPs display a single standard delivered price |
| SHIP-P1-004 | Preserve paid premium delivery options | VOPS, ENG-WEB | Not started | Same-day/fixed-time/midnight fees are explicit and optional |
| SHIP-P1-005 | Add a 30–60 day `Free standard delivery` launch campaign | GROWTH, CAT | Not started | Campaign applies only to commercially approved products |
| SHIP-P1-006 | Prevent shipping from exceeding 12% of promoted product value without an explicit exception | CAT, VOPS | Not started | Automated/reporting check identifies no unapproved violation |
| SHIP-P1-007 | Remove `Delivery calculated at checkout` where delivery is already known | ENG-WEB | Not started | PDP, drawer, and cart show consistent delivery information |

## P1: Delivery Promise and Reminder Alignment

Target completion: within two weeks.

| ID | Action | Owner | Status | Completion test |
| --- | --- | --- | --- | --- |
| DEL-P1-001 | Configure explicit delivery lead time for every promoted product | VOPS, CAT | Not started | No hero product uses the generic five-day fallback |
| DEL-P1-002 | Configure vendor same-day cutoffs where supported | VOPS | Not started | Same-day eligibility changes correctly after cutoff |
| DEL-P1-003 | Make delivery estimates pincode-aware | ENG-BE, ENG-WEB | Not started | Displayed date reflects the selected destination |
| DEL-P1-004 | Recommend only products deliverable before the occasion | ENG-BE | Not started | Recommendation filter rejects late products |
| DEL-P1-005 | Schedule reminders using lead time plus a two-day decision buffer | ENG-BE, GROWTH | Not started | Reminder timing always precedes the latest safe order date |
| DEL-P1-006 | Add `Order within X hours for delivery by DATE` where SLA supports it | ENG-WEB, VOPS | Not started | Countdown and date use the same cutoff source |
| DEL-P1-007 | Record dispatch and delivery timestamps | ENG-BE, OPS | Not started | On-time delivery can be calculated per vendor |
| DEL-P1-008 | Define automatic/manual pause criteria for repeated late fulfilment | VOPS, DATA | Not started | Vendor/product is paused after the agreed failure threshold |

## P2: Positioning and Trust

Target completion: within four weeks.

| ID | Action | Owner | Status | Completion test |
| --- | --- | --- | --- | --- |
| POS-P2-001 | Adopt the gifting-concierge positioning statement | GROWTH | Not started | Homepage, WhatsApp, ads, and lifecycle copy use one consistent promise |
| POS-P2-002 | Replace `Start gifting` with a decision-oriented CTA such as `Find the right gift` | GROWTH, ENG-WEB | Not started | Primary CTA matches concierge positioning |
| POS-P2-003 | Present Safe, Personal, and Premium choices | GROWTH, CAT, ENG-WEB | Not started | Shortlists consistently contain the three roles where inventory permits |
| POS-P2-004 | Make physical fulfilment explicitly Pune-first during the pilot | GROWTH, VOPS | Not started | Marketing claims match actual reliable serviceability |
| POS-P2-005 | Remove purchase claims for unavailable cakes, gift cards, experiences, chocolates, or personalised products | GROWTH, ENG-WEB | Not started | Public purchase claims map to a working checkout capability |
| POS-P2-006 | Replace generic partner trust copy with verifiable fulfilment information | ENG-WEB, VOPS | Not started | PDP shows fulfilment identity/status, policy links, and delivery SLA |
| POS-P2-007 | Collect reviews only from completed orders | OPS, ENG-BE, ENG-WEB | Not started | Every published review is linked internally to a delivered order |
| POS-P2-008 | Add real gifting case studies by recipient, budget, and deadline | GROWTH | Not started | At least five evidence-based stories are published |

Recommended trust block:

- Fulfilled by `[vendor/partner name]`
- KYC verified
- Serviceable to `[pincode/city]`
- Earliest delivery `[date]`
- Standard delivery `[included/amount]`
- Wishkr support and resolution policy

## P2: Analytics and Decision Dashboard

Target completion: before paid acquisition or a major public launch.

### Required commerce funnel

1. Product viewed
2. Pincode checked
3. Added to cart
4. Cart viewed
5. Checkout started
6. Quote succeeded or failed
7. Checkout viewed
8. Payment opened
9. Payment completed
10. Vendor acknowledged
11. Order dispatched
12. Order delivered
13. Delivered on time

### Required breakdowns

- Product
- Vendor
- Category
- Product price
- Delivered price
- Shipping amount
- Promised delivery days
- Destination city/pincode group
- Traffic source and campaign
- Device class
- Checkout failure reason

### Analytics actions

| ID | Action | Owner | Status | Completion test |
| --- | --- | --- | --- | --- |
| DATA-P2-001 | Build one daily commerce-funnel dashboard | DATA | Not started | Every funnel stage is visible by day and source |
| DATA-P2-002 | Verify existing Vercel Analytics/GA4 events are arriving | DATA, ENG-WEB | Not started | Test session appears through purchase completion |
| DATA-P2-003 | Add `out_of_stock_product_viewed` | ENG-WEB | Not started | Event includes product and source placement |
| DATA-P2-004 | Add `empty_category_viewed` | ENG-WEB | Not started | Event records category and navigation source |
| DATA-P2-005 | Add `pincode_unavailable` | ENG-WEB | Not started | Event records product, vendor, and coarse destination |
| DATA-P2-006 | Add `mixed_vendor_cart_blocked` | ENG-WEB | Not started | Event records both vendor IDs without customer data |
| DATA-P2-007 | Add structured quote failure reasons | ENG-BE, ENG-WEB | Not started | Failures separate stock, vendor, shipping, price, and validation problems |
| DATA-P2-008 | Add delivery-date rejection tracking | ENG-BE, ENG-WEB | Not started | Rejection records requested versus earliest date |
| DATA-P2-009 | Verify Razorpay cancellation and verification-failure tracking | ENG-WEB, ENG-BE | Not started | Payment abandonment is distinguishable from application failure |
| DATA-P2-010 | Add vendor acknowledgement and fulfilment timing metrics | ENG-BE, VOPS | Not started | Vendor response, dispatch, and delivery SLA can be reported |
| DATA-P2-011 | Classify traffic-versus-conversion weekly | DATA, GROWTH | Not started | Weekly report states whether the limiting stage is qualified visits, PDP engagement, cart, checkout, or payment |
| DATA-P2-012 | Generate and reconcile shop/product campaign links using the [campaign tracking standard](SHOP_CAMPAIGN_TRACKING.md) | GROWTH, ENG-WEB, DATA | Implemented locally; awaiting deploy and GA4 verification | An attributed test order matches its admin first/last touch and GA4 product purchase |

Privacy rules:

- Do not send names, phone numbers, email addresses, street addresses, gift
  messages, or order confirmation tokens to analytics.
- Use product ID, vendor ID, category, coarse location, price, fee, delivery
  days, status, and anonymised session identifiers.

## Rollout Plan

### Phase 1: Stabilise

Target: days 1–3.

- Complete all P0 storefront actions.
- Complete vendor readiness review.
- Classify existing orders.
- Run test orders.
- Confirm that no empty category or unavailable product is promoted.

### Phase 2: Rebuild the offer

Target: days 4–14.

- Curate the 30–40 product hero catalogue.
- Fix delivered pricing and shipping.
- Configure SLAs and cutoffs.
- Build the Pune fast-delivery collection.
- Restore categories only after inventory is present.

### Phase 3: Reposition and measure

Target: days 15–30.

- Launch gifting-concierge messaging.
- Connect reminders to three purchasable choices.
- Complete the analytics dashboard.
- Publish trust information and first verified case studies.
- Start controlled acquisition only after end-to-end verification.

### Phase 4: Scale carefully

Target: after day 30.

- Add cities only when a repeatable vendor SLA exists.
- Add vendors only through the readiness gate.
- Expand categories based on observed conversion, not catalogue size.
- Keep premium products in intentional premium collections.
- Review shipping, cancellation, and on-time delivery by vendor every week.

## Launch Criteria

Do not aggressively market physical-gift checkout until all critical criteria
are true.

### Storefront

- [ ] No unavailable product is promoted
- [ ] No empty category is advertised
- [ ] No unjustified bestseller label is visible
- [ ] PDP, cart, checkout, and Razorpay totals agree
- [ ] Mixed-vendor carts cannot fail unexpectedly
- [ ] Pincode is entered once and reused
- [ ] Delivery copy is accurate and non-contradictory

### Catalogue

- [ ] 30–40 strong products are actively promoted
- [ ] At least 10 Pune products support verified same- or next-day delivery
- [ ] Hero products have complete images, inclusions, packaging, and policies
- [ ] Mainstream price bands are adequately stocked
- [ ] Premium products are separated from everyday gift discovery

### Vendors

- [ ] Every live vendor has completed KYC
- [ ] Every live vendor has complete policies
- [ ] Every promoted product has a configured SLA
- [ ] Every vendor has passed a test order
- [ ] Inventory and acknowledgement processes are working

### Analytics

- [ ] Complete funnel is measurable
- [ ] Checkout and payment failures are classified
- [ ] Shipping and delivery-day effects can be compared
- [ ] Vendor fulfilment and on-time delivery can be measured
- [ ] Analytics contains no customer personal data

## Weekly Operating Review

Review these metrics every week:

| Area | Metric |
| --- | --- |
| Acquisition | Qualified shop sessions by source |
| Discovery | Product views per session |
| Suitability | Product-view-to-add-to-cart rate |
| Serviceability | Pincode success rate |
| Checkout | Cart-to-checkout and quote success rates |
| Payment | Payment-opened-to-paid rate |
| Shipping | Conversion by fee percentage and delivered-price band |
| Delivery | Conversion by promised lead time |
| Vendor operations | Acknowledgement time, dispatch time, on-time delivery |
| Catalogue quality | Out-of-stock exposure, duplicates, missing content |
| Customer trust | Support contacts, cancellations, refunds, verified reviews |

Each review should produce:

1. The largest current funnel loss.
2. The products/vendors responsible for that loss.
3. One operational fix.
4. One customer-facing experiment.
5. A clear keep, change, pause, or remove decision.

## Out of Scope

This plan does not authorise:

- Deleting production product or order data.
- Changing vendor payouts without commercial approval.
- Promising guaranteed delivery without a verified SLA.
- Publishing customer or order details.
- Scaling paid acquisition before funnel measurement is working.
- Enabling national physical fulfilment before local operations are reliable.

## Change Log

| Date | Change | Owner |
| --- | --- | --- |
| 2026-07-29 | Initial action plan created from production catalogue, order, vendor, shipping, delivery, storefront, and competitor analysis | Codex |
