# Budget bouquet images and PyariSakhee catalogue audit

Date: 2 August 2026

## Launch-image candidates

Five square, 1254 × 1254 PNG concept images were generated for the new House of Zahraa draft products. The visible primary-flower counts match the stored recipes.

| Draft product | Price | Image | Visible composition |
|---|---:|---|---|
| Sunshine Mini | ₹349 | [`sunshine-mini.png`](../assets/product-images/budget-bouquets/sunshine-mini.png) | Exactly 1 yellow sunflower, sparse filler, foliage, kraft wrap |
| Carnation Pocket Bouquet | ₹399 | [`carnation-pocket-bouquet.png`](../assets/product-images/budget-bouquets/carnation-pocket-bouquet.png) | Exactly 5 carnations: 3 blush pink and 2 white |
| Gerbera Pop Bouquet | ₹449 | [`gerbera-pop-bouquet.png`](../assets/product-images/budget-bouquets/gerbera-pop-bouquet.png) | Exactly 5 gerberas: 2 yellow, 2 orange and 1 coral pink |
| Petite Seasonal Mix | ₹479 | [`petite-seasonal-mix.png`](../assets/product-images/budget-bouquets/petite-seasonal-mix.png) | Exactly 1 sunflower, 2 gerberas and 2 carnations |
| Three-Rose Mini | ₹449 | [`three-rose-mini.png`](../assets/product-images/budget-bouquets/three-rose-mini.png) | Exactly 3 red roses and foliage |

These are concept renders, not photographs of completed vendor stock. Before publishing, House of Zahraa should confirm that its stem colours, wrapping, filler density and finished bouquet size can match the images. If not, photograph the real samples and replace the concepts. Keep the products in draft until that confirmation.

## Reusable image-generation direction

Generate one image per product. Use a square, photorealistic ecommerce composition with a warm ivory seamless studio background, soft diffused daylight, a subtle grounded shadow and the full bouquet centred in a three-quarter front view. The bouquet must look like a believable compact Indian florist product. Do not include people, hands, vases, props, text, logos, watermarks, borders, extra blooms or hidden buds.

The product-specific instructions are:

1. **Sunshine Mini:** Exactly one yellow sunflower as the only focal bloom, sparse tiny neutral seasonal filler sprigs, light foliage, one layer of natural kraft paper and simple twine.
2. **Carnation Pocket Bouquet:** Exactly five visibly countable carnations—three blush pink and two soft white—with modest foliage, no filler blossoms, one sheet of warm ivory paper and twine.
3. **Gerbera Pop Bouquet:** Exactly five visibly countable gerberas—two yellow, two orange and one coral pink—with modest foliage, no other flowers, matte white paper and twine.
4. **Petite Seasonal Mix:** Exactly five primary blooms—one yellow sunflower, two gerberas (orange and coral pink), and two carnations (blush pink and white)—plus sparse tiny filler and foliage in matte cream paper.
5. **Three-Rose Mini:** Exactly three visibly countable red roses, no buds or other flowers, modest foliage, natural kraft paper and twine.

## PyariSakhee repeated-name audit

Production contains 23 PyariSakhee product rows across seven case-insensitive exact repeated-name groups. A broader title scan found one additional Ditya colourway hidden by the typo `Dutya`, plus three already colour-labelled Tulip bracelet rows. None of PyariSakhee's 102 products reuse the same primary image URL, and none of the exact repeated-name rows currently has a `product_variants` record.

There are no confirmed true duplicate rows in these seven groups. The apparent duplicates fall into two categories: colourways of one base design, or unrelated designs with an overly generic shared title.

### Same design, different colours

Keep these purchasable. Do not delete them.

| Current name | Product ID | Visual colourway | Recommended immediate name |
|---|---|---|---|
| Anvita Jhumki Earrings | `prd_ps_757e840bb95c4421bf5f` | Amber and red stones | Anvita Jhumki Earrings – Amber & Red |
| Anvita Jhumki Earrings | `prd_ps_ad560e9fdaad4325b2be` | Pink and green stones | Anvita Jhumki Earrings – Pink & Green |
| Anvita Jhumki Earrings | `prd_ps_cbb7371d8d524cca91d1` | Red and green stones | Anvita Jhumki Earrings – Red & Green |
| Ditya Jhumki Earrings | `prd_ps_539bb2440daa4d62b079` | Mint centre with ruby stones | Ditya Jhumki Earrings – Mint & Ruby |
| Ditya Jhumki Earrings | `prd_ps_d613abf31ed84062924c` | Blush centre with aqua stones | Ditya Jhumki Earrings – Blush & Aqua |
| Dutya Jhumki Earrings | `prd_ps_8c2bc4a5f9894599a50d` | Mustard centre with green stones | Ditya Jhumki Earrings – Mustard & Green |

The long-term catalogue model should merge each design into one product with colour variants. Do not merge yet: `product_variants` has no image or image-gallery field, so buyers would not see the correct image when changing colour. Until variant-specific images and storefront image switching exist, keep separate rows and add the colour to each title. Correct `Dutya` to `Ditya` as part of the rename.

Two additional colour families already use distinguishable titles:

- Three Tulip bracelet rows are Pink/Purple/Clear, Red/Green and Royal Red versions of the same base design. Keep them separate until variant images are supported, but correct `bracelate` to `bracelet` and standardise the title format.
- `prd_ps_117efa8e29954f3688ac` (Stretch Crystal Rhinestone Bracelet) says Beige, Black, Golden and Silver are available and shows them in its gallery, but it has neither variants nor a customization choice. Buyers currently have no explicit way to select a colour. Add a required colour selector before promoting this listing.

### Different products sharing a generic title

These are not duplicates and should not be merged.

| Current group | Product ID | What the image actually shows | Recommended name |
|---|---|---|---|
| Hoops earrings | `prd_ps_254da2fd18e346a6a2cc` | Puffy heart hoops | Puffy Heart Hoop Earrings |
| Hoops earrings | `prd_ps_3aaacef09ac34934a4e2` | Pointed teardrop hoops | Pointed Teardrop Hoop Earrings |
| Hoops earrings | `prd_ps_6907b22128f0415c8827` | Sculptural twisted hoops | Sculptural Twist Hoop Earrings |
| Hoops earrings | `prd_ps_7c141a4ced2840bb9b7a` | Hammered heart hoops | Hammered Heart Hoop Earrings |
| Hoops earrings | `prd_ps_87ed07994335442c9304` | Hammered oval hoops | Hammered Oval Hoop Earrings |
| Hoops earrings | `prd_ps_8c8df5d7540c415dab55` | Open heart-outline hoops | Open Heart Hoop Earrings |
| Hoops earrings | `prd_ps_e0494d200bfc486aac0f` | Chunky crescent hoops | Chunky Crescent Hoop Earrings |
| Hoops earrings | `prd_ps_f0df411dd6654ab3b2b2` | Geometric trapezoid hoops | Geometric Trapezoid Hoop Earrings |
| Hoops earrings | `prd_ps_f6c381768c914515ac6b` | Textured leaf hoops | Textured Leaf Hoop Earrings |
| Hoops earrings | `prd_ps_fadc7308302644fab56f` | Linked-heart hoops | Linked Heart Hoop Earrings |
| Flower celestial ring | `prd_ps_3bd2f098d60e432e81e8` | Open leaf-vine band | Open Leaf Vine Ring |
| Flower celestial ring | `prd_ps_d5c60badede1454aa7e2` | Raised flower-cluster band | Golden Flower Cluster Ring |
| korean earrings | `prd_ps_2e06df229a5f49eeb67d` | Mother-of-pearl flower studs | Mother-of-Pearl Flower Studs |
| Korean Earrings | `prd_ps_97fcc7d14f454904bf85` | Two-tone shell drops | Two-Tone Shell Drop Earrings |
| Mayur Stud Earrings | `prd_ps_a29071032afa431daa4f` | Multicolour stone peacock studs | Multicolour Mayur Stud Earrings |
| Mayur Stud Earrings | `prd_ps_d23e3d669f60400db950` | Large oxidised peacock design | Oxidised Peacock Statement Studs |
| Minimal celestial ring | `prd_ps_b38b2f4651914e0b871e` | Interlocking-circle openwork band | Interlocking Circle Openwork Ring |
| Minimal celestial ring | `prd_ps_dca0581055d44d2e85ca` | Leaf openwork band | Minimal Leaf Openwork Ring |

### Recommended action order

1. Rename the repeated-title rows and the `Dutya` typo so colour or design is visible in shop cards, search results, order lines and admin; also standardise the three Tulip bracelet titles.
2. Keep all rows; do not run a duplicate deletion or merge based only on normalized product name.
3. Add variant-specific primary image and gallery support to the product-variant model, admin editor and storefront.
4. After image switching is supported, consolidate only Anvita and Ditya into colour variants, preserving old URLs with redirects or canonical handling.
5. Review the ring descriptions while renaming: several current “celestial” descriptions do not accurately identify the pictured leaf/flower designs.
