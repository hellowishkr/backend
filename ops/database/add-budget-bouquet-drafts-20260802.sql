\set ON_ERROR_STOP on

-- Five budget bouquet concepts requested for House of Zahraa on 2026-08-02.
-- They are deliberately non-orderable drafts until exact product photography,
-- substitution rules, procurement costs, and peak-season costs are approved.
--
-- The supplied "maximum target recipe cost" is stored conservatively as the
-- exact normal recipe cost.  A visible other-cost contingency closes the gap
-- between the itemised estimate and the ceiling, so admin margin calculations
-- do not assume that every rupee of the ceiling will remain unused.

BEGIN;
SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;

DO $$
BEGIN
  IF (SELECT COUNT(*) FROM vendors WHERE id = 'ven_1783308098_069b48b8') <> 1 THEN
    RAISE EXCEPTION 'House of Zahraa vendor target is missing or ambiguous';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM products AS p
    JOIN (
      VALUES
        ('prd_hz_sunshine_mini_260802', 'Sunshine Mini'),
        ('prd_hz_carnation_pocket_260802', 'Carnation Pocket Bouquet'),
        ('prd_hz_gerbera_pop_260802', 'Gerbera Pop Bouquet'),
        ('prd_hz_petite_mix_260802', 'Petite Seasonal Mix'),
        ('prd_hz_three_rose_260802', 'Three-Rose Mini')
    ) AS expected(id, name)
      ON expected.name = p.name
    WHERE p.vendor_id = 'ven_1783308098_069b48b8'
      AND p.id <> expected.id
  ) THEN
    RAISE EXCEPTION 'A budget bouquet name already exists under a different product id';
  END IF;
END $$;

INSERT INTO products (
  id,
  vendor_id,
  name,
  description,
  category,
  occasions,
  preparation_time,
  dispatch_lead_time_days,
  delivery_pincodes,
  is_bestseller,
  is_customisable,
  supports_gift_message,
  ships_nationally,
  product_images,
  care_instructions,
  whats_included,
  customization_schema,
  base_price_paise,
  discounted_price_paise,
  inventory_quantity,
  gst_rate,
  hsn,
  shipping_included,
  shipping_fee_paise,
  image_url,
  video_url,
  active,
  in_stock,
  lifecycle_status
)
VALUES
(
  'prd_hz_sunshine_mini_260802',
  'ven_1783308098_069b48b8',
  'Sunshine Mini',
  'Budget bouquet concept for Pune delivery. Draft until the exact sunflower variety, seasonal filler substitution policy, product photography, and vendor costs are approved.',
  'Flowers',
  '["birthday","congratulations","thank-you","just-because"]'::json,
  '1',
  0,
  '[]'::json,
  false,
  true,
  true,
  false,
  '[]'::json,
  'Keep in a cool place. Trim the stem and place it in fresh water as soon as possible.',
  '1 sunflower, seasonal fillers, kraft wrap',
  '[]'::json,
  34900,
  NULL,
  0,
  NULL,
  NULL,
  true,
  0,
  NULL,
  NULL,
  false,
  false,
  'draft'
),
(
  'prd_hz_carnation_pocket_260802',
  'ven_1783308098_069b48b8',
  'Carnation Pocket Bouquet',
  'Budget bouquet concept for Pune delivery. Draft until carnation colours, foliage substitutions, product photography, and vendor costs are approved.',
  'Flowers',
  '["birthday","congratulations","thank-you","just-because"]'::json,
  '1',
  0,
  '[]'::json,
  false,
  true,
  true,
  false,
  '[]'::json,
  'Keep in a cool place. Trim the stems and place them in fresh water as soon as possible.',
  '5 carnations, foliage, single-sheet wrap',
  '[]'::json,
  39900,
  NULL,
  0,
  NULL,
  NULL,
  true,
  0,
  NULL,
  NULL,
  false,
  false,
  'draft'
),
(
  'prd_hz_gerbera_pop_260802',
  'ven_1783308098_069b48b8',
  'Gerbera Pop Bouquet',
  'Budget bouquet concept for Pune delivery. Draft until gerbera colours, foliage substitutions, product photography, and vendor costs are approved.',
  'Flowers',
  '["birthday","congratulations","thank-you","just-because"]'::json,
  '1',
  0,
  '[]'::json,
  false,
  true,
  true,
  false,
  '[]'::json,
  'Keep in a cool place. Trim the stems and place them in fresh water as soon as possible.',
  '5 gerberas, foliage, simple wrap',
  '[]'::json,
  44900,
  NULL,
  0,
  NULL,
  NULL,
  true,
  0,
  NULL,
  NULL,
  false,
  false,
  'draft'
),
(
  'prd_hz_petite_mix_260802',
  'ven_1783308098_069b48b8',
  'Petite Seasonal Mix',
  'Budget mixed-flower concept for Pune delivery. Draft until the focal flower and substitution policy, product photography, and vendor costs are approved.',
  'Flowers',
  '["birthday","congratulations","thank-you","just-because"]'::json,
  '1',
  0,
  '[]'::json,
  false,
  true,
  true,
  false,
  '[]'::json,
  'Keep in a cool place. Trim the stems and place them in fresh water as soon as possible.',
  '2 gerberas, 2 carnations, 1 focal flower, seasonal fillers, simple wrap',
  '[]'::json,
  47900,
  NULL,
  0,
  NULL,
  NULL,
  true,
  0,
  NULL,
  NULL,
  false,
  false,
  'draft'
),
(
  'prd_hz_three_rose_260802',
  'ven_1783308098_069b48b8',
  'Three-Rose Mini',
  'Budget rose concept for Pune delivery. Its target cost assumes stems are allocated from shared 20-rose procurement lots; it must remain draft until shared inventory handling, product photography, and vendor costs are approved.',
  'Flowers',
  '["birthday","anniversary","thank-you","just-because","romantic-surprise"]'::json,
  '1',
  0,
  '[]'::json,
  false,
  true,
  true,
  false,
  '[]'::json,
  'Keep in a cool place. Trim the stems and place them in fresh water as soon as possible.',
  '3 roses, foliage, basic wrap',
  '[]'::json,
  44900,
  NULL,
  0,
  NULL,
  NULL,
  true,
  0,
  NULL,
  NULL,
  false,
  false,
  'draft'
)
ON CONFLICT (id) DO NOTHING;

INSERT INTO product_variants (
  id,
  product_id,
  name,
  sku,
  price_delta_paise,
  discounted_price_paise,
  inventory_quantity,
  seasonal_price_paise,
  seasonal_price_start,
  seasonal_price_end,
  bouquet_recipe,
  active,
  sort_order
)
VALUES
(
  'var_hz_sunshine_mini_260802',
  'prd_hz_sunshine_mini_260802',
  'Standard',
  'HZ-SUNSHINE-MINI',
  34900,
  NULL,
  0,
  NULL,
  NULL,
  NULL,
  json_build_object(
    'enabled', true,
    'components', json_build_array(
      json_build_object('name', 'Sunflower', 'kind', 'flower', 'quantity', 1, 'unit_label', 'stem', 'lot_size', 1, 'off_season_lot_cost_paise', 3500, 'peak_season_lot_cost_paise', NULL),
      json_build_object('name', 'Seasonal fillers', 'kind', 'filler', 'quantity', 1, 'unit_label', 'set', 'lot_size', 1, 'off_season_lot_cost_paise', 1000, 'peak_season_lot_cost_paise', NULL),
      json_build_object('name', 'Kraft wrap', 'kind', 'packaging', 'quantity', 1, 'unit_label', 'sheet', 'lot_size', 1, 'off_season_lot_cost_paise', 1500, 'peak_season_lot_cost_paise', NULL)
    ),
    'making_cost_paise', 3000,
    'packaging_cost_paise', 0,
    'other_cost_paise', 2000
  ),
  false,
  1
),
(
  'var_hz_carnation_pocket_260802',
  'prd_hz_carnation_pocket_260802',
  'Standard',
  'HZ-CARNATION-POCKET',
  39900,
  NULL,
  0,
  NULL,
  NULL,
  NULL,
  json_build_object(
    'enabled', true,
    'components', json_build_array(
      json_build_object('name', 'Carnation', 'kind', 'flower', 'quantity', 5, 'unit_label', 'stem', 'lot_size', 1, 'off_season_lot_cost_paise', 1000, 'peak_season_lot_cost_paise', NULL),
      json_build_object('name', 'Foliage', 'kind', 'filler', 'quantity', 1, 'unit_label', 'set', 'lot_size', 1, 'off_season_lot_cost_paise', 1000, 'peak_season_lot_cost_paise', NULL),
      json_build_object('name', 'Single-sheet wrap', 'kind', 'packaging', 'quantity', 1, 'unit_label', 'sheet', 'lot_size', 1, 'off_season_lot_cost_paise', 2000, 'peak_season_lot_cost_paise', NULL)
    ),
    'making_cost_paise', 4000,
    'packaging_cost_paise', 0,
    'other_cost_paise', 2000
  ),
  false,
  1
),
(
  'var_hz_gerbera_pop_260802',
  'prd_hz_gerbera_pop_260802',
  'Standard',
  'HZ-GERBERA-POP',
  44900,
  NULL,
  0,
  NULL,
  NULL,
  NULL,
  json_build_object(
    'enabled', true,
    'components', json_build_array(
      json_build_object('name', 'Gerbera', 'kind', 'flower', 'quantity', 5, 'unit_label', 'stem', 'lot_size', 1, 'off_season_lot_cost_paise', 1500, 'peak_season_lot_cost_paise', NULL),
      json_build_object('name', 'Foliage', 'kind', 'filler', 'quantity', 1, 'unit_label', 'set', 'lot_size', 1, 'off_season_lot_cost_paise', 1000, 'peak_season_lot_cost_paise', NULL),
      json_build_object('name', 'Simple bouquet wrap', 'kind', 'packaging', 'quantity', 1, 'unit_label', 'set', 'lot_size', 1, 'off_season_lot_cost_paise', 2000, 'peak_season_lot_cost_paise', NULL)
    ),
    'making_cost_paise', 4500,
    'packaging_cost_paise', 0,
    'other_cost_paise', 2000
  ),
  false,
  1
),
(
  'var_hz_petite_mix_260802',
  'prd_hz_petite_mix_260802',
  'Standard',
  'HZ-PETITE-SEASONAL-MIX',
  47900,
  NULL,
  0,
  NULL,
  NULL,
  NULL,
  json_build_object(
    'enabled', true,
    'components', json_build_array(
      json_build_object('name', 'Gerbera', 'kind', 'flower', 'quantity', 2, 'unit_label', 'stem', 'lot_size', 1, 'off_season_lot_cost_paise', 1500, 'peak_season_lot_cost_paise', NULL),
      json_build_object('name', 'Carnation', 'kind', 'flower', 'quantity', 2, 'unit_label', 'stem', 'lot_size', 1, 'off_season_lot_cost_paise', 1000, 'peak_season_lot_cost_paise', NULL),
      json_build_object('name', 'Seasonal focal flower', 'kind', 'flower', 'quantity', 1, 'unit_label', 'stem', 'lot_size', 1, 'off_season_lot_cost_paise', 3500, 'peak_season_lot_cost_paise', NULL),
      json_build_object('name', 'Seasonal fillers', 'kind', 'filler', 'quantity', 1, 'unit_label', 'set', 'lot_size', 1, 'off_season_lot_cost_paise', 1000, 'peak_season_lot_cost_paise', NULL),
      json_build_object('name', 'Simple bouquet wrap', 'kind', 'packaging', 'quantity', 1, 'unit_label', 'set', 'lot_size', 1, 'off_season_lot_cost_paise', 2500, 'peak_season_lot_cost_paise', NULL)
    ),
    'making_cost_paise', 5000,
    'packaging_cost_paise', 0,
    'other_cost_paise', 2000
  ),
  false,
  1
),
(
  'var_hz_three_rose_260802',
  'prd_hz_three_rose_260802',
  'Standard',
  'HZ-THREE-ROSE-MINI',
  44900,
  NULL,
  0,
  NULL,
  NULL,
  NULL,
  json_build_object(
    'enabled', true,
    'components', json_build_array(
      -- ₹10 per stem is an allocation from a shared ₹200 / 20-stem lot.  It
      -- is not valid if a fresh supplier lot is bought for every order.
      json_build_object('name', 'Rose (shared-lot allocation)', 'kind', 'flower', 'quantity', 3, 'unit_label', 'stem', 'lot_size', 1, 'off_season_lot_cost_paise', 1000, 'peak_season_lot_cost_paise', 3000),
      json_build_object('name', 'Foliage', 'kind', 'filler', 'quantity', 1, 'unit_label', 'set', 'lot_size', 1, 'off_season_lot_cost_paise', 1000, 'peak_season_lot_cost_paise', NULL),
      json_build_object('name', 'Basic bouquet wrap', 'kind', 'packaging', 'quantity', 1, 'unit_label', 'set', 'lot_size', 1, 'off_season_lot_cost_paise', 2500, 'peak_season_lot_cost_paise', NULL)
    ),
    'making_cost_paise', 4500,
    'packaging_cost_paise', 0,
    'other_cost_paise', 6000
  ),
  false,
  1
)
ON CONFLICT (id) DO NOTHING;

DO $$
DECLARE
  expected record;
  actual_cost integer;
BEGIN
  FOR expected IN
    SELECT *
    FROM (
      VALUES
        ('prd_hz_sunshine_mini_260802', 'var_hz_sunshine_mini_260802', 34900, 11000),
        ('prd_hz_carnation_pocket_260802', 'var_hz_carnation_pocket_260802', 39900, 14000),
        ('prd_hz_gerbera_pop_260802', 'var_hz_gerbera_pop_260802', 44900, 17000),
        ('prd_hz_petite_mix_260802', 'var_hz_petite_mix_260802', 47900, 19000),
        ('prd_hz_three_rose_260802', 'var_hz_three_rose_260802', 44900, 17000)
    ) AS valueset(product_id, variant_id, sale_price_paise, max_recipe_cost_paise)
  LOOP
    IF NOT EXISTS (
      SELECT 1
      FROM products
      WHERE id = expected.product_id
        AND vendor_id = 'ven_1783308098_069b48b8'
        AND category = 'Flowers'
        AND base_price_paise = expected.sale_price_paise
        AND active = false
        AND in_stock = false
        AND inventory_quantity = 0
        AND lifecycle_status = 'draft'
        AND dispatch_lead_time_days = 0
        AND shipping_included = true
        AND COALESCE(shipping_fee_paise, 0) = 0
        AND COALESCE(json_array_length(product_images), 0) = 0
    ) THEN
      RAISE EXCEPTION 'Draft product % does not match its guarded state', expected.product_id;
    END IF;

    IF NOT EXISTS (
      SELECT 1
      FROM product_variants
      WHERE id = expected.variant_id
        AND product_id = expected.product_id
        AND price_delta_paise = expected.sale_price_paise
        AND inventory_quantity = 0
        AND active = false
        AND bouquet_recipe ->> 'enabled' = 'true'
        AND json_array_length(bouquet_recipe -> 'components') > 0
    ) THEN
      RAISE EXCEPTION 'Draft variant % does not match its guarded state', expected.variant_id;
    END IF;

    SELECT
      COALESCE((
        SELECT SUM(
          (
            ((component ->> 'quantity')::integer + (component ->> 'lot_size')::integer - 1)
            / (component ->> 'lot_size')::integer
          ) * (component ->> 'off_season_lot_cost_paise')::integer
        )
        FROM json_array_elements(variant.bouquet_recipe -> 'components') AS component
      ), 0)::integer
      + COALESCE((variant.bouquet_recipe ->> 'making_cost_paise')::integer, 0)
      + COALESCE((variant.bouquet_recipe ->> 'packaging_cost_paise')::integer, 0)
      + COALESCE((variant.bouquet_recipe ->> 'other_cost_paise')::integer, 0)
    INTO actual_cost
    FROM product_variants AS variant
    WHERE variant.id = expected.variant_id;

    IF actual_cost <> expected.max_recipe_cost_paise THEN
      RAISE EXCEPTION
        'Recipe % costs %, expected target ceiling %',
        expected.variant_id,
        actual_cost,
        expected.max_recipe_cost_paise;
    END IF;
  END LOOP;
END $$;

COMMIT;

WITH recipe_costs AS (
  SELECT
    variant.id,
    material.material_cost_paise
    + COALESCE((variant.bouquet_recipe ->> 'making_cost_paise')::integer, 0)
    + COALESCE((variant.bouquet_recipe ->> 'packaging_cost_paise')::integer, 0)
    + COALESCE((variant.bouquet_recipe ->> 'other_cost_paise')::integer, 0) AS recipe_cost_paise
  FROM product_variants AS variant
  CROSS JOIN LATERAL (
    SELECT COALESCE(SUM(
      (
        ((component ->> 'quantity')::integer + (component ->> 'lot_size')::integer - 1)
        / (component ->> 'lot_size')::integer
      ) * (component ->> 'off_season_lot_cost_paise')::integer
    ), 0)::integer AS material_cost_paise
    FROM json_array_elements(variant.bouquet_recipe -> 'components') AS component
  ) AS material
  WHERE variant.id IN (
    'var_hz_sunshine_mini_260802',
    'var_hz_carnation_pocket_260802',
    'var_hz_gerbera_pop_260802',
    'var_hz_petite_mix_260802',
    'var_hz_three_rose_260802'
  )
)
SELECT
  product.id,
  product.name,
  product.lifecycle_status,
  product.active,
  product.in_stock,
  product.base_price_paise,
  recipe_costs.recipe_cost_paise,
  variant.active AS variant_active,
  variant.inventory_quantity AS variant_inventory,
  json_array_length(product.product_images) AS image_count
FROM products AS product
JOIN product_variants AS variant ON variant.product_id = product.id
JOIN recipe_costs ON recipe_costs.id = variant.id
WHERE product.id IN (
  'prd_hz_sunshine_mini_260802',
  'prd_hz_carnation_pocket_260802',
  'prd_hz_gerbera_pop_260802',
  'prd_hz_petite_mix_260802',
  'prd_hz_three_rose_260802'
)
ORDER BY product.base_price_paise, product.name;
