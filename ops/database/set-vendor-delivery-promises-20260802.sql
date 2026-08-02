-- Production delivery promises requested on 2026-08-02.
--
-- The storefront adds one calendar day of delivery safety after the configured
-- product dispatch lead time. Therefore:
--   * House of Zahraa: 0 dispatch days => next-calendar-day delivery
--   * PyariSakhee Jewellery: 1 dispatch day => second-calendar-day delivery
--
-- Product values are authoritative; vendor values provide the same fallback
-- for any newly-created product that has not set a product-specific lead time.

BEGIN;

DO $$
BEGIN
  IF (SELECT COUNT(*) FROM vendors WHERE id = 'ven_1783308098_069b48b8') <> 1 THEN
    RAISE EXCEPTION 'House of Zahraa vendor target is missing or ambiguous';
  END IF;

  IF (SELECT COUNT(*) FROM vendors WHERE id = 'ven_1782830020_c1f45d4d') <> 1 THEN
    RAISE EXCEPTION 'PyariSakhee vendor target is missing or ambiguous';
  END IF;

  IF (SELECT COUNT(*) FROM products WHERE vendor_id = 'ven_1783308098_069b48b8') = 0 THEN
    RAISE EXCEPTION 'House of Zahraa has no products to update';
  END IF;

  IF (
    SELECT COUNT(*)
    FROM products
    WHERE vendor_id = 'ven_1782830020_c1f45d4d'
      AND lower(category) = 'jewellery'
  ) = 0 THEN
    RAISE EXCEPTION 'PyariSakhee has no Jewellery products to update';
  END IF;
END $$;

UPDATE vendors
SET delivery_lead_time_hours = 0
WHERE id = 'ven_1783308098_069b48b8';

UPDATE products
SET dispatch_lead_time_days = 0,
    updated_at = now()
WHERE vendor_id = 'ven_1783308098_069b48b8';

UPDATE vendors
SET delivery_lead_time_hours = 24
WHERE id = 'ven_1782830020_c1f45d4d';

UPDATE products
SET dispatch_lead_time_days = 1,
    updated_at = now()
WHERE vendor_id = 'ven_1782830020_c1f45d4d'
  AND lower(category) = 'jewellery';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM products
    WHERE vendor_id = 'ven_1783308098_069b48b8'
      AND dispatch_lead_time_days IS DISTINCT FROM 0
  ) THEN
    RAISE EXCEPTION 'House of Zahraa delivery update did not cover every product';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM products
    WHERE vendor_id = 'ven_1782830020_c1f45d4d'
      AND lower(category) = 'jewellery'
      AND dispatch_lead_time_days IS DISTINCT FROM 1
  ) THEN
    RAISE EXCEPTION 'Jewellery delivery update did not cover every product';
  END IF;
END $$;

COMMIT;

SELECT
  v.name AS vendor,
  v.delivery_lead_time_hours,
  COUNT(p.id) AS products,
  MIN(p.dispatch_lead_time_days) AS min_dispatch_days,
  MAX(p.dispatch_lead_time_days) AS max_dispatch_days
FROM vendors v
JOIN products p ON p.vendor_id = v.id
WHERE v.id IN ('ven_1783308098_069b48b8', 'ven_1782830020_c1f45d4d')
GROUP BY v.id, v.name, v.delivery_lead_time_hours
ORDER BY v.name;
