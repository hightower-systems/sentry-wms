-- avid-overhaul-mk1 P6.1: granular per-page permissions for web-admin
-- USERs. The existing role model has just ADMIN and USER; USER could
-- never reach the admin surface (every admin endpoint required ADMIN).
-- The new junction table lets an ADMIN grant a USER access to a
-- specific subset of pages.
--
-- Page keys are URL slugs from the React admin (e.g. 'items',
-- 'purchase-orders'). They live in api/constants.py as ALL_PAGE_KEYS
-- and the column has no CHECK constraint here so adding a new page
-- only requires a constants.py update plus a row insert; no schema
-- migration for routine page additions.
--
-- ADMIN role bypasses this table entirely - admins have full access
-- by definition. Only USER role rows look up here.

CREATE TABLE IF NOT EXISTS user_page_permissions (
    user_id     INT          NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    page_key    VARCHAR(64)  NOT NULL,
    granted_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    granted_by  INT          REFERENCES users(user_id) ON DELETE SET NULL,
    PRIMARY KEY (user_id, page_key)
);

CREATE INDEX IF NOT EXISTS ix_user_page_permissions_user
    ON user_page_permissions(user_id);
