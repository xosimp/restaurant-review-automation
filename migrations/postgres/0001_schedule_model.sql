-- 0001 — the normalized schedule model (scheduler audit 10/10/26 RM-13,
-- ARCH-34; arch.md Part 11, scale.md "Data model"). In its own schema,
-- `sched`, beside the copied tables: written by the shadow (pg_shadow) from
-- every schedule revision while SQLite is the primary, compared against the
-- source (pg_shadow.compare), and the shape the readers move to after the
-- cutover. People are the existing `people` table (people.id = person_id).
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE SCHEMA IF NOT EXISTS sched;

-- One week of one restaurant (= schedule_history.id): the working revision
-- (what the Studio edits) and the published one (what staff were sent).
CREATE TABLE IF NOT EXISTS sched.schedule_weeks (
    id                     BIGINT PRIMARY KEY,
    restaurant_id          BIGINT NOT NULL,
    week_start             DATE NOT NULL,
    week_end               DATE,
    working_revision_id    BIGINT,
    published_revision_id  BIGINT,
    superseded_by          BIGINT,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sched_weeks_rid_start ON sched.schedule_weeks (restaurant_id, week_start);

-- Immutable revisions (= schedule_versions.id): generated, edited, fixes,
-- swap, published, rename …, each with its parent and whose hand it was.
CREATE TABLE IF NOT EXISTS sched.schedule_revisions (
    id              BIGINT PRIMARY KEY,
    week_id         BIGINT NOT NULL REFERENCES sched.schedule_weeks (id) ON DELETE CASCADE,
    restaurant_id   BIGINT NOT NULL,
    version         INTEGER NOT NULL,
    parent_version  INTEGER,
    kind            TEXT NOT NULL,
    author          TEXT,
    authority       TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (week_id, version)
);
CREATE INDEX IF NOT EXISTS sched_revisions_rid ON sched.schedule_revisions (restaurant_id, created_at);

-- Every shift of every revision, by its stable shift_id (contract #3), on
-- real time (timestamptz, the restaurant's zone, overnight closes on the
-- next day). A double booking cannot be stored in a published revision:
-- the exclusion constraint refuses one person on two overlapping shifts.
CREATE TABLE IF NOT EXISTS sched.shifts (
    revision_id      BIGINT NOT NULL REFERENCES sched.schedule_revisions (id) ON DELETE CASCADE,
    shift_id         TEXT NOT NULL,
    week_id          BIGINT NOT NULL,
    restaurant_id    BIGINT NOT NULL,
    person_id        BIGINT,
    employee         TEXT NOT NULL,
    role             TEXT,
    business_date    DATE NOT NULL,
    start_ts         TIMESTAMPTZ NOT NULL,
    end_ts           TIMESTAMPTZ NOT NULL,
    scheduled_hours  NUMERIC,
    notes            TEXT,
    locked           BOOLEAN NOT NULL DEFAULT false,
    published        BOOLEAN NOT NULL DEFAULT false,
    span             TSTZRANGE GENERATED ALWAYS AS (tstzrange(start_ts, end_ts, '[)')) STORED,
    PRIMARY KEY (revision_id, shift_id),
    CONSTRAINT sched_shifts_no_double_booking
        EXCLUDE USING gist (revision_id WITH =, person_id WITH =, span WITH &&)
        WHERE (person_id IS NOT NULL AND published)
);
CREATE INDEX IF NOT EXISTS sched_shifts_person ON sched.shifts (restaurant_id, person_id, business_date);

-- The append-only event log: one row per shift operation a revision made
-- (add, remove, change) and per generate / publish, in order per week —
-- UNIQUE (week_id, seq) is the optimistic concurrency `expected_version`
-- does today, and a diff is a slice of the range.
CREATE TABLE IF NOT EXISTS sched.schedule_events (
    week_id        BIGINT NOT NULL REFERENCES sched.schedule_weeks (id) ON DELETE CASCADE,
    seq            BIGINT NOT NULL,
    restaurant_id  BIGINT NOT NULL,
    revision_id    BIGINT NOT NULL,
    actor          TEXT,
    authority      TEXT,
    kind           TEXT NOT NULL,
    payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (week_id, seq)
);
CREATE INDEX IF NOT EXISTS sched_events_rid ON sched.schedule_events (restaurant_id, created_at);

-- Headline scores only; the 100–440 KB explanation is a blob (blob_store).
CREATE TABLE IF NOT EXISTS sched.schedule_scores (
    revision_id  BIGINT PRIMARY KEY REFERENCES sched.schedule_revisions (id) ON DELETE CASCADE,
    score        NUMERIC,
    band         TEXT,
    detail_ref   TEXT
);
