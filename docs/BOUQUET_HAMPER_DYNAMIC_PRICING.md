# Bouquet and Hamper Configuration Plan

## Document status

- **Prepared:** 30 July 2026
- **Last production update:** 2 August 2026
- **Vendor audited:** `house.ofzahraa`
- **Production access:** Guarded production reads and transactional catalogue updates completed
- **Implementation status:** Pricing model is deployed; all 14 active vendor products have recipe coverage
- **Repositories:** `wishkr-backend`, `wishkr-admin`, and `wishkr-web`

## Decision

Wishkr should offer bouquet sizes such as 20, 40, 60, or 100 flowers as
buyer-selectable variants of one bouquet style. Each option has its own:

- customer price and inventory;
- flower/material composition;
- supplier lot size and off-season/peak lot cost;
- making, packaging, and other cost;
- optional festive price and date window.

The recipe is an admin costing tool. The customer sees only the option name,
composition, availability, and final price. Supplier costs and Wishkr margin
are never included in the public catalogue response.

This model also supports hampers. Flowers, chocolates, teddy bears, balloons,
boxes, and packaging can all be recipe materials in the same option.

## Production catalogue audit

The original 30 July audit found 14 active House of Zaharaa products with no
variants or add-ons. Eight products stated a usable quantity in their catalogue
copy, while six required the vendor to confirm their complete composition.
As of 2 August, the catalogue has active 20/40/60 rose-size pilots and inactive
recipe drafts. The inactive drafts are visible to admin users but excluded
from public catalogue responses and checkout.

### Recommendation table applied on 2 August 2026

No existing customer-facing fixed price was changed: the six exact fixed
prices already matched the recommendation. Exact-count recipes were added as
inactive variants with zero inventory so operations can complete missing
costs and images before deciding whether to activate them.

| Product | Production result |
| --- | --- |
| Luxury 3 Ft 150 Pink Roses | Live fixed price remains ₹4,999; inactive 150-rose signature recipe added |
| Pink 200 Roses Bouquet | Live fixed price remains ₹5,999; inactive 200-rose signature recipe added; it remains separate from the 150-rose product |
| Premium Red & White Heart | Live fixed price remains ₹4,999; inactive 100 red + 60 white signature recipe and ₹2,999 50 + 30 half-size costing draft added |
| Red/White Rose & Chocolate Heart | Live fixed price remains ₹5,999; inactive 50 red + 25 white + 30 Ferrero signature recipe added; no smaller option added |
| 200 Rose Gift Box | Live fixed price remains ₹4,499; inactive 200-rose gift-box recipe added |
| Pink/White Ferrero Hamper | Live fixed price remains ₹3,899; inactive 20 pink + 20 white + 16 Ferrero signature recipe added; the separate ₹1,899 15-rose benchmark also remains an inactive draft |
| Lilly Roses Mix | Inactive current-photo draft added with red/white lily arrangements, buds, foliage, baby’s breath, pastel wrap, jute mesh, and ribbon; counts remain pending |
| Lilly and Pink Roses | Inactive current-photo draft records pink lilies, roses, carnations, fillers, and packaging; separate ₹2,699 16-rose + 3-lily benchmark remains inactive and needs its own photo |
| Teddy Mix Flower Bouquet | Inactive current-photo ₹2,499 recipe records 20 pink/blush roses and a pink teddy; separate ₹1,699 roses/teddy/chocolate benchmark remains inactive and needs its own photo |
| Mix Flowers Hamper | Inactive current-photo recipe records lilies, sunflowers, roses, carnations, chrysanthemums, orchids, fillers, hatbox, and tags; exact stem counts remain pending |
| Teddy Hamper | Inactive current-photo recipe records the large teddy, rose basket arrangement, red fillers, two heart balloons, basket, and ribbon; rose count and all costs remain pending |
| Birthday Combo | Inactive current-photo recipe records two floral arrangements, three LED bubble balloons, three heart balloons, five latex balloons, stand, and presentation materials; flower counts and costs remain pending |

All draft recipes use the confirmed rose procurement lot of 20 stems: ₹200
per lot normally and ₹600 per lot at peak. Unconfirmed lilies, chocolates,
teddy bears, bases, wrapping, making, packaging, and delivery inputs are
currently ₹0 placeholders. A draft must not be activated until those fields
are replaced with approved costs and its photograph accurately represents the
option.

### Pune delivery status

House of Zaharaa is configured as a local Pune vendor. Its bouquet products do
not ship nationally, Pune is its verified operating city, product shipping is
included, the vendor has no flat shipping override, and the extra-item fee is
₹0. The resulting checkout shipping charge is therefore ₹0 for serviceable
Pune orders.

Production verification on 2 August confirmed that pincode `411001` is
serviceable and that an authoritative bouquet quote returns
`shipping.fee_paise = 0` with charge type `free`. No additional database change
was required for free Pune bouquet delivery.

### Full recipe and photography audit

On 2 August, all 14 primary product images and 10 alternate images were
reviewed against the product name, description, and “What’s included” text.
Every active House of Zaharaa product now has at least one admin recipe. There
are 22 recipe options in total: six active rose-size options and 16 inactive
current-product, signature, or benchmark drafts.

| Product family | Recipe basis | Activation status or blocker |
| --- | --- | --- |
| Red Roses Rounded | Exact 20/40/60 rose counts plus photographed white filler and black wrap | Three options active; each size still needs size-accurate photography |
| Roses Heart-Shaped | Exact 20/40/60 rose counts plus heart support, wrap, ribbon, bow, and tag | Three options active; each size still needs size-accurate photography |
| 150 Pink Roses | Written 150-rose count agrees with the photograph; filler, structure, border wrap, and ribbon recorded | Inactive until all non-rose costs are entered |
| 200 Pink Roses | Written 200-rose count agrees with the photograph; white filler, pink wrap, and bow recorded | Inactive; peak rose cost alone reaches ₹6,000 |
| Premium Red/White Heart | Written 100 red + 60 white counts agree with the photograph; baby’s breath and presentation materials recorded | Signature and half-size drafts inactive; half-size needs its own photograph |
| Red/White Chocolate Heart | Written 50 red + 25 white + 30 Ferrero agrees with the photograph | Inactive until chocolate, structure, wrapping, and labour costs are entered |
| 200-Rose Gift Box | Written text promises a gift box, but current photography looks like a wrapped heart bouquet | Inactive; correct the photograph or the product promise before activation |
| Pink/White Ferrero | Written text promises 20 pink + 20 white roses + 16 Ferrero, while photographs visibly include lilies/chrysanthemums and do not depict that exact promise | Inactive; requires a replacement image or corrected written specification |
| Lilly Roses Mix | Photography depicts red and white lilies, buds, baby’s breath, and foliage; roses are not visibly established | Inactive; rename/correct copy and confirm stem counts |
| Lilly and Pink Roses | Photography depicts pink lilies, pink roses, pink carnations, baby’s breath, buds, foliage, and layered packaging | Current recipe and competitor benchmark inactive; confirm counts and provide an exact image per option |
| Mix Flowers Hamper | Photography supports lilies, sunflowers, roses, carnations, chrysanthemums, orchids, fillers, hatbox, and tags | Inactive; confirm each stem count and substitution policy |
| Teddy Hamper | Photography supports one large teddy, a red-rose basket, red filler, and two heart balloons | Inactive; confirm rose count, teddy size/SKU, basket, balloon, and labour costs |
| Teddy Mix Flower | Photography supports 20 pink/blush roses, one pink teddy, foliage, wrap, and ribbon; no chocolates are shown | Current recipe inactive; chocolate benchmark needs separate photography |
| Birthday Combo | Photography consistently supports a large bouquet, floral box, three LED bubble balloons, three heart balloons, and five latex balloons; cake and branded gift tray appear only in lifestyle photos and are not in “What’s included” | Inactive; exclude photo-only props or explicitly add and cost them before activation |

For unconfirmed flower counts, recipes use one `arrangement` unit at ₹0 rather
than inventing stem quantities. This preserves the material checklist without
creating false cost or customer promises. Operations must replace each such
line with a confirmed quantity, lot size, and cost before activation.

### Original products ready for initial recipe configuration (30 July baseline)

| Product ID | Current product | Current price | Known composition | Recommended configuration |
| --- | --- | ---: | --- | --- |
| `prd_1783318182_fabd4f63` | 40 Red Roses Bouquet | ₹1,999 | 40 red roses | Make this a red-rose bouquet family with 20 and 40 as the initial options |
| `prd_1783313547_dfd9a284` | 50 Roses Heart-Shaped Bouquet | ₹3,999 | 50 red roses | Keep as a separate heart arrangement; confirm which other counts preserve the design |
| `prd_1783321368_37000e95` | Luxury 3 Ft 150 Pink Roses Bouquet | ₹4,999 | 150 pink roses | Premium pink-rose family; add only sizes for which the photograph and dimensions remain accurate |
| `prd_1783321966_811cfce9` | Luxury Heart Red Rose & Chocolate Bouquet | ₹5,999 | 50 red roses, 25 white roses, 30 Ferrero Rocher | Composite bouquet recipe; flower and chocolate inputs are costed independently |
| `prd_1783317473_8640b47c` | Pink 200 Roses Bouquet | ₹5,999 | 200 pink roses | Pink-rose bouquet family; reconcile with the existing 150-rose listing before merging |
| `prd_1783310702_72bd6699` | Premium Heart Red & White Bouquet | ₹4,999 | 100 red roses, 60 white roses | Separate heart-style family with fixed red:white ratios |
| `prd_1783311873_5cee6830` | Heart-Shaped Red Rose Gift Box Hamper | ₹4,499 | 200 red roses | Hamper recipe with roses plus the box and all packaging materials |
| `prd_1783312316_1faaaf63` | Pink & White Rose Ferrero Hamper | ₹3,899 | 20 pink roses, 20 white roses, 16 Ferrero Rocher | Best first hamper pilot because all important quantities are stated |

### Original products requiring vendor confirmation (30 July baseline)

| Product ID | Current product | Current price | Missing information |
| --- | --- | ---: | --- |
| `prd_1783315570_04fd2637` | Lilly Roses Mix Flower Bouquet | ₹3,499 | Lily count, rose count, colours, fillers, wrapping, and making cost |
| `prd_1783317054_4a5c7df3` | Lilly and Pink Roses Bouquet | ₹3,999 | Lily count, rose count, fillers, wrapping, and making cost |
| `prd_1783319114_c96ec457` | Teddy Mix Flower Bouquet | ₹2,499 | Flower counts, teddy cost, fillers, wrapping, and making cost |
| `prd_1783313230_b36abebc` | Birthday Combo | ₹9,999 | Lily bouquet recipe, lily hamper recipe, balloon-stand materials, and labour |
| `prd_1783312539_4cc6a185` | Mix Flowers Hamper | ₹2,699 | Every flower type/count, hamper container, fillers, and labour |
| `prd_1783316588_c60bcf33` | Teddy Hamper | ₹3,999 | Rose count, teddy specification/cost, balloon count, basket, and labour |

Do not estimate the missing quantities from photographs. The vendor must
confirm them because an incorrect recipe produces an incorrect seasonal cost,
customer promise, and margin.

## How pricing works

### Cost calculation

For each recipe material:

```text
required lots = round up(material quantity / supplier lot size)
material cost = required lots × selected lot cost

recipe cost =
  all material costs
  + making cost
  + packaging cost
  + other cost
```

Roses are initially configured with the vendor-confirmed lot size of 20.
Therefore, a 50-rose option is conservatively costed as three lots, not 2.5
lots. This prevents hidden procurement loss. If the vendor later shares
leftover stock across several orders, that should be modelled as a separate
procurement/inventory improvement.

### Example using the discussed rose costs

The following uses **illustrative** making and packaging costs of ₹100 and ₹90.
Vendor operations must replace them with confirmed amounts.

| Option | Normal raw flowers | Peak raw flowers at ₹30/stem | Total normal cost | Total peak cost | Proposed normal sale price | Normal contribution | Peak result at normal price |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 20 roses | ₹200 | ₹600 | ₹390 | ₹790 | ₹499 | ₹109 (21.8%) | ₹291 loss |
| 40 roses | ₹400 | ₹1,200 | ₹590 | ₹1,390 | ₹799 | ₹209 (26.2%) | ₹591 loss |

This shows why ₹499 and ₹799 cannot remain active during a high flower-cost
period. Before delivery, payment fees, support cost, or tax, the 20-rose peak
recipe already costs ₹790 and the 40-rose recipe costs ₹1,390.

A useful selling-price floor is:

```text
minimum customer price =
  (recipe cost + allocated delivery + other per-order cost)
  / (1 - target contribution margin)
```

The admin should show the cost and margin, but an operator must approve the
customer price. The system does not silently derive and publish a selling
price from vendor costs.

### Customer-price precedence

1. If today is inside the option's seasonal date window, use its seasonal
   price.
2. Otherwise, use its normal sale price when present.
3. Otherwise, use its normal list/base price.
4. A separately configured global Wishkr sale can still apply afterward.

The backend repeats this calculation during checkout. A customer cannot force
an old or lower price by modifying browser data.

## Admin workflow

For a bouquet or hamper product:

1. Open **Products** and edit the product.
2. Open **Bouquet builder**.
3. Add the first buyer option, for example `20 roses`.
4. Add each material and specify quantity, unit, lot size, normal lot cost,
   and optional peak lot cost.
5. Enter making, packaging, and other costs.
6. Set the normal customer price and inspect the normal margin.
7. Set the festive customer price and date range, then inspect the peak margin.
8. Duplicate the option to create 40, 60, or 100 flowers and adjust its recipe.
9. Enter option-level inventory and make only orderable options active.
10. Save the product and verify the public product page and checkout quote.

The default first-option values reflect the discussion—20 red roses, a ₹200
normal lot, a ₹600 peak lot, and a ₹499 selling price—but are editable.

## Storefront behaviour

- Product cards show the cheapest available option as `From ₹…`.
- The product page shows a dedicated **Choose your bouquet size** grid.
- Each option shows its name, safe composition summary, exact customer price,
  seasonal state, and sold-out state.
- Choosing another option updates the product total.
- Inventory is enforced per option.
- Checkout reloads the variant and effective price from the database.

The public response may show `40 Red roses`. It never exposes lot costs,
making cost, packaging cost, peak cost, or margin.

## Data model

Each buyer option remains a `product_variants` row. The following fields were
added:

| Field | Purpose |
| --- | --- |
| `seasonal_price_paise` | Complete customer selling price during the seasonal window |
| `seasonal_price_start` | Optional inclusive start date |
| `seasonal_price_end` | Optional inclusive end date |
| `bouquet_recipe` | Admin-only JSON recipe and cost inputs |

Example recipe:

```json
{
  "enabled": true,
  "components": [
    {
      "name": "Red rose",
      "kind": "flower",
      "quantity": 20,
      "unit_label": "stem",
      "lot_size": 20,
      "off_season_lot_cost_paise": 20000,
      "peak_season_lot_cost_paise": 60000
    }
  ],
  "making_cost_paise": 10000,
  "packaging_cost_paise": 9000,
  "other_cost_paise": 0
}
```

## Catalogue-structure recommendations

- Use variants only when the bouquet's design, colours, packaging, and
  customer expectation remain substantially the same at a different size.
- Keep heart-shaped, three-foot, teddy, and gift-box designs as separate
  product families.
- Merge the 150- and 200-pink-rose listings only after confirming that they
  use the same design and imagery can accurately show both options.
- Model mixed-flower bouquets as a fixed recipe per option. Do not let a buyer
  change flower counts freely until substitution and inventory rules exist.
- For hampers, treat chocolates, teddy bears, balloons, baskets, and boxes as
  separate materials. Their costs do not scale automatically with flower
  count.
- Start with a small set of clear options. `20 roses` and `40 roses` are more
  understandable than a long list of nearly identical choices.

## Action items

| ID | Action | Owner | Status | Completion test |
| --- | --- | --- | --- | --- |
| BOUQ-001 | Audit House of Zaharaa bouquet and hamper catalogue | CAT, ENG-BE | Complete | All 14 active products are classified by recipe readiness |
| BOUQ-002 | Add recipe, seasonal price, and date-window fields to the database/API | ENG-BE | Complete in production | Migration applies and admin values round-trip |
| BOUQ-003 | Add admin bouquet/hamper pricing builder with cost and margin preview | ENG-ADMIN | Implemented and build verified | Admin can create, duplicate, cost, schedule, and disable buyer options |
| BOUQ-004 | Show bouquet-size choices and composition on the product page | ENG-WEB | Implemented | Buyer can choose an available option and sees its exact price |
| BOUQ-005 | Enforce seasonal option price in backend checkout | ENG-BE | Complete | Quote test proves the database price overrides browser snapshots |
| BOUQ-006 | Confirm missing compositions for six products | VOPS, house.ofzahraa | Waiting on vendor | Every material, quantity, and unit is confirmed in writing |
| BOUQ-007 | Confirm rose/lily lot sizes, normal/peak costs, making, packaging, delivery, tax, and target margin | VOPS, FINANCE | Not started | Every configured option has an approved non-loss price |
| BOUQ-008 | Decide which existing listings become one option family and prepare size-accurate images | CAT, house.ofzahraa | Not started | Each variant family has consistent design and honest imagery |
| BOUQ-009 | Apply migration and deploy backend, admin, and web changes | ENG | Backend production migration complete; verify every frontend deployment | All three production services run the same release |
| BOUQ-010 | Configure the 20/40 red-rose pilot and the Pink & White Rose Ferrero Hamper | CAT, VOPS | Rose pilots active; Ferrero recipe drafts inactive | Both products have verified costs, inventory, options, images, and seasonal prices |
| BOUQ-011 | Place one normal-price and one seasonal-price test order | OPS, ENG | Not started | PDP, cart, checkout, payment, and stored order totals match |
| BOUQ-012 | Review conversion, gross contribution, cancellations, and substitutions weekly | DATA, VOPS | Not started | Option-level results are reviewed before adding more sizes |

## Production rollout gate

Do not activate an inactive production draft until:

- the migration and all three applications are deployed together;
- the vendor confirms the complete material recipe;
- making, packaging, delivery, payment, and tax costs are included in the
  commercial check;
- each option has an accurate photograph or clearly labelled representative
  image;
- normal and festive inventory processes are agreed;
- one end-to-end test order has passed.

Production writes are limited to the documented active rose pilots and
inactive, zero-inventory costing drafts. The recommendation-table transaction
is preserved in
`ops/database/add-recommendation-table-draft-variants-20260802.sql`; the three
FNP benchmark drafts are preserved in
`ops/database/add-fnp-benchmark-draft-variants-20260802.sql`; and the full
catalogue recipe/photo reconciliation is preserved in
`ops/database/complete-house-of-zahraa-recipes-20260802.sql`.
