# Bouquet and Hamper Configuration Plan

## Document status

- **Prepared:** 30 July 2026
- **Vendor audited:** `house.ofzahraa`
- **Production access:** Read-only catalogue analysis
- **Implementation status:** Implemented locally; production migration and catalogue configuration are not yet applied
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

House of Zaharaa currently has 14 active products, with no variants or add-ons.
Eight products state a usable quantity in their catalogue copy. Six require
the vendor to confirm their complete composition before they can be priced
safely.

### Products ready for initial recipe configuration

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

### Products requiring vendor confirmation

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
| BOUQ-002 | Add recipe, seasonal price, and date-window fields to the database/API | ENG-BE | Implemented locally | Migration applies and admin values round-trip |
| BOUQ-003 | Add admin bouquet/hamper pricing builder with cost and margin preview | ENG-ADMIN | Implemented locally | Admin can create, duplicate, cost, schedule, and disable buyer options |
| BOUQ-004 | Show bouquet-size choices and composition on the product page | ENG-WEB | Implemented locally | Buyer can choose an available option and sees its exact price |
| BOUQ-005 | Enforce seasonal option price in backend checkout | ENG-BE | Implemented locally | Quote test proves the database price overrides browser snapshots |
| BOUQ-006 | Confirm missing compositions for six products | VOPS, house.ofzahraa | Waiting on vendor | Every material, quantity, and unit is confirmed in writing |
| BOUQ-007 | Confirm rose/lily lot sizes, normal/peak costs, making, packaging, delivery, tax, and target margin | VOPS, FINANCE | Not started | Every configured option has an approved non-loss price |
| BOUQ-008 | Decide which existing listings become one option family and prepare size-accurate images | CAT, house.ofzahraa | Not started | Each variant family has consistent design and honest imagery |
| BOUQ-009 | Apply migration and deploy backend, admin, and web changes | ENG | Awaiting approval | All three production services run the same release |
| BOUQ-010 | Configure the 20/40 red-rose pilot and the Pink & White Rose Ferrero Hamper | CAT, VOPS | Blocked by BOUQ-007/009 | Both products have verified costs, inventory, options, images, and seasonal prices |
| BOUQ-011 | Place one normal-price and one seasonal-price test order | OPS, ENG | Not started | PDP, cart, checkout, payment, and stored order totals match |
| BOUQ-012 | Review conversion, gross contribution, cancellations, and substitutions weekly | DATA, VOPS | Not started | Option-level results are reviewed before adding more sizes |

## Production rollout gate

Do not configure production variants until:

- the migration and all three applications are deployed together;
- the vendor confirms the complete material recipe;
- making, packaging, delivery, payment, and tax costs are included in the
  commercial check;
- each option has an accurate photograph or clearly labelled representative
  image;
- normal and festive inventory processes are agreed;
- one end-to-end test order has passed.

No production database write was made during this analysis or implementation.
