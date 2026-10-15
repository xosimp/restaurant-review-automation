-- 0000 — what the app's SQLite-era SQL reads, on Postgres (scheduler audit
-- 10/10/26 RM-13; db_dialect). Applied first by scripts/run_pg_copy.py.
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

-- cavnar_ts: SQLite's reading of a date/time text (date(x), datetime(x),
-- julianday(x), strftime(f, x) — db_dialect._timestamp). SQLite answers
-- NULL for '' or text that is no date; a bare CAST raised, so one dirty
-- stamp failed the whole query (blind re-audit 10/11/26, SCALE-9). An
-- offset is honoured and the answer is UTC, as SQLite's is (the session
-- runs in UTC: db_dialect._driver_connect).
CREATE OR REPLACE FUNCTION cavnar_ts(v text) RETURNS timestamp
    LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN v ~ '^\s*[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])([ T]([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9](\.[0-9]+)?)?)?\s*(Z|[+-][0-9]{2}(:?[0-9]{2})?)?\s*$'
                THEN timezone('UTC', CAST(v AS timestamptz)) END
$$;

-- cav_name_key: staff_settings.name_key, the one rule that makes a typed
-- name one person (whitespace collapsed, case folded). On SQLite people.py
-- registers the Python function per connection; on Postgres it is this
-- (db_dialect.SQL_PORTED_FUNCTIONS). Python's casefold() also folds a few
-- characters lower() keeps (German ß): names differing only there match in
-- Python and not here.
CREATE OR REPLACE FUNCTION cav_name_key(v text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT lower(regexp_replace(btrim(coalesce(v, ''), E' \t\n\r\f\v'), E'[\\s]+', ' ', 'g'))
$$;

-- cavnar_json_array: the array a json_each reads, or none — db_dialect
-- turns json_each(doc, '$.path') into jsonb_array_elements_text over this.
CREATE OR REPLACE FUNCTION cavnar_json_array(v jsonb) RETURNS jsonb
    LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN jsonb_typeof(v) = 'array' THEN v ELSE '[]'::jsonb END
$$;
