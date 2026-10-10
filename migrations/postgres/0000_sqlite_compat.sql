-- 0000 — what the app's SQLite-era SQL reads, on Postgres (scheduler audit
-- 10/10/26 RM-13; db_dialect). Applied first by scripts/pg_copy.py.
--
-- sqlite_master: retention's "does this table exist", the scrub plan, the
-- reference schema and delete_restaurant's table walk read it (17 sites);
-- here it is a view over the catalog with SQLite's columns. A month
-- partition of a fact table is not listed (its parent is).
CREATE OR REPLACE VIEW sqlite_master AS
    SELECT 'table'::text AS type, c.table_name::text AS name, c.table_name::text AS tbl_name,
           0 AS rootpage, NULL::text AS sql
      FROM information_schema.tables c
     WHERE c.table_schema = current_schema() AND c.table_type = 'BASE TABLE'
       AND NOT EXISTS (SELECT 1 FROM pg_inherits h
                        WHERE h.inhrelid = to_regclass(quote_ident(c.table_name)))
    UNION ALL
    SELECT 'index'::text, i.indexname::text, i.tablename::text, 0, i.indexdef::text
      FROM pg_indexes i
     WHERE i.schemaname = current_schema();
