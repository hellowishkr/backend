# Wishkr Shop Campaign and Product Tracking

## Status

- **Prepared:** 30 July 2026
- **Scope:** `wishkr-web`, `wishkr-backend`, and `wishkr-admin`
- **Implementation:** Complete locally; awaiting deployment and production analytics verification

## Recommended link

The supplied Instagram influencer link follows the correct convention:

```text
https://www.wishkr.com/shop?utm_source=instagram&utm_medium=influencer&utm_campaign=shubham_aug2026&utm_content=story1
```

Use it when the influencer is promoting the overall shop. When a campaign is
promoting one product, link directly to that product instead:

```text
https://www.wishkr.com/shop/40-red-roses-bouquet-prd_1783318182_fabd4f63?utm_source=instagram&utm_medium=influencer&utm_campaign=shubham_aug2026&utm_content=story1
```

A direct product link provides a cleaner measurement path from campaign click
to product view, add to cart, checkout, and purchase.

## Admin workflow

1. Open **Admin → Campaigns**.
2. Find **Shop tracking link**.
3. Choose **Main shop** or **Specific product**.
4. If it is a product campaign, search for and select the product.
5. Enter source, medium, campaign, content, and optional term.
6. Copy the generated link.
7. Open **Test link** in an incognito window before publishing it.

The builder automatically creates the canonical product path and safely
URL-encodes every parameter.

## UTM naming standard

| Parameter | Meaning | Example | Required |
| --- | --- | --- | --- |
| `utm_source` | Platform, publisher, or partner | `instagram` | Yes |
| `utm_medium` | Traffic arrangement | `influencer` | Yes |
| `utm_campaign` | Promotion and time period | `shubham_aug2026` | Yes |
| `utm_content` | Exact creative or placement | `story1`, `reel2`, `bio_link` | Recommended |
| `utm_term` | Audience, keyword, or offer segment | `pune_gifts` | Optional |

Rules:

- Use lowercase `snake_case`.
- Keep the same source, medium, and campaign across one promotion.
- Give every story, reel, post, bio link, and ad creative a different
  `utm_content`.
- Never put a person's name, phone, email, order ID, address, or other personal
  information into a UTM value.
- Do not rename a campaign after links have been published.

## Attribution behaviour

Wishkr records:

- **First touch:** the visitor's first captured landing.
- **Last touch:** the latest tagged campaign landing.

A later direct visit does not erase the latest campaign touch. A later tagged
link updates last touch while preserving first touch.

The browser stores the attribution through browsing and checkout. When an
order is created, the backend validates and saves the safe attribution fields
in `orders.marketing_attribution`.

Stored fields are limited to:

- UTM values;
- landing path;
- referrer origin and path, without its query string;
- capture time.

Customer names, phones, addresses, and gift messages are not analytics
attribution fields.

## Product funnel

| Funnel stage | Internal event | GA4 ecommerce event | Product data |
| --- | --- | --- | --- |
| Product page opened | `product_viewed` | `view_item` | Product ID, category, price |
| Product added | `add_to_cart` | `add_to_cart` | Product ID, category, price |
| Cart opened | `cart_viewed` | `view_cart` | All product IDs, variants, quantities, prices |
| Checkout started | `checkout_started` | `begin_checkout` | All product IDs, variants, quantities, prices |
| Payment completed | `purchase_completed` | `purchase` | Order ID, total, and all purchased product lines |

Every mapped event also carries the latest:

- campaign source;
- campaign medium;
- campaign name;
- campaign content;
- campaign term.

This permits reporting by influencer, campaign, creative, product, and order.

## Where to review results

### Wishkr Admin

The Orders list shows the latest source/medium and campaign/content. Opening an
order shows both first and last touch, including the landing page and capture
time.

This is the authoritative answer to:

> Which campaign was stored with this actual Wishkr order?

### GA4

Use:

- **Reports → Acquisition → Traffic acquisition** for source/medium and
  campaign;
- **Reports → Monetization → Ecommerce purchases** for product revenue;
- **Explore** to compare campaign or content against `view_item`,
  `add_to_cart`, `begin_checkout`, and `purchase`.

The GTM container must forward the following data-layer fields to the GA4
event tag:

- `campaign_source`
- `campaign_medium`
- `campaign_name`
- `campaign_content`
- `campaign_term`
- `items`
- `value`
- `currency`
- `transaction_id`

Verify the mapping in GA4 DebugView after deployment. GTM container settings
are external configuration and are not changed by the repository deployment.

### Vercel Analytics

Wishkr's internal event stream receives the scalar UTM values on every tracked
event. Use it for quick funnel checks; use the saved order attribution and GA4
for revenue reconciliation.

## Revenue query

The following PostgreSQL query groups product orders by their saved last-touch
campaign:

```sql
SELECT
  COALESCE(marketing_attribution->'last_touch'->>'utm_source', 'direct') AS source,
  COALESCE(marketing_attribution->'last_touch'->>'utm_medium', 'none') AS medium,
  COALESCE(marketing_attribution->'last_touch'->>'utm_campaign', 'unattributed') AS campaign,
  COALESCE(marketing_attribution->'last_touch'->>'utm_content', 'unspecified') AS content,
  COUNT(*) AS orders_created,
  COUNT(*) FILTER (WHERE status IN ('PAID', 'PROCESSING', 'COMPLETED')) AS paid_orders,
  SUM(total_amount) FILTER (WHERE status IN ('PAID', 'PROCESSING', 'COMPLETED')) AS paid_revenue
FROM orders
WHERE order_type = 'product'
GROUP BY 1, 2, 3, 4
ORDER BY paid_revenue DESC NULLS LAST;
```

## Production verification

1. Deploy the web, backend, and admin changes.
2. Confirm the marketing-attribution migration is applied.
3. Generate a test link from admin using `utm_content=tracking_test`.
4. Open it in an incognito window.
5. Verify `view_item` and `add_to_cart` in GA4 DebugView.
6. Complete a low-value test order.
7. Confirm the purchase contains the correct GA4 item and transaction ID.
8. Open the order in admin and confirm first/last touch.
9. Run the revenue query and confirm the test campaign appears.
10. Exclude or refund the test order before commercial reporting.

