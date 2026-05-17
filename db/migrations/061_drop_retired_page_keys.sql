-- avid-overhaul-mk1 P10: retire the admin-side picking/packing/shipping
-- page mirrors. The actual workflow lives on the Chainway C6000 mobile
-- scanners; the admin pages duplicated state without adding control
-- surface, so they were removed in the same phase.
--
-- The user_page_permissions table has no CHECK constraint on page_key
-- (deliberately - see mig 059 header), so stale rows for retired keys
-- linger without preventing reads. Clearing them here keeps the
-- permission grid in the Users page in sync with ALL_PAGE_KEYS and
-- prevents a "ghost" grant from re-appearing if the keys are ever
-- re-introduced.
--
-- Idempotent: WHERE filters by the retired set; a re-run finds zero
-- rows and is a no-op.

DELETE FROM user_page_permissions
 WHERE page_key IN ('picking', 'packing', 'shipping');
