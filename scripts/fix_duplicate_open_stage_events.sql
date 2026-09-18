-- Clear the 8 duplicate open stage events that block tracker/0022.
--
-- WHY THIS EXISTS
-- ---------------
-- `tracker/0022_stageevent_tracker_stage_event_one_open_visit_per_stage` adds
--
--     UNIQUE (invoice, stage)
--     WHERE exited_at IS NULL AND event_type <> 'NOTE'
--
-- "an invoice has at most one open visit per stage". Live violates it on 8
-- (invoice, stage) pairs, so the migration fails with
--
--     could not create unique index "tracker_stage_event_one_open_visit_per_stage"
--     DETAIL: Key (invoice_id, stage_id)=(64, 3) is duplicated.
--
-- The 8 pairs are two different problems, and they need different fixes.
--
-- Run once on live, BEFORE `manage.py migrate`. Idempotent: re-running finds
-- nothing left to fix.

BEGIN;

-- ---------------------------------------------------------------------------
-- A. Six legacy note artifacts  (invoices 64, 309, 401, 402, 404, 405)
--
-- These are precisely the bug the NOTE event type was introduced to fix, as
-- StageEvent.EventType.NOTE's own docstring describes: an annotation used to be
-- written as RECEIVE with `entered_at` COPIED FROM THE VISIT and no
-- `exited_at`, so the stage showed two rows that both looked like an open
-- visit, tied on the only column anything ordered by.
--
-- They are identifiable exactly as that description implies: the two open rows
-- share an identical `entered_at`. The annotation is the one written later
-- (higher id) -- on 64 and 309 it is the row carrying the HOLD status and the
-- remark, while the plain receive has neither.
--
-- Converting it to NOTE is what today's code would have written: the constraint
-- ignores NOTE rows, the remark stays visible, and the dwell clock keeps
-- running on the real visit row. Nothing is deleted and nothing is closed --
-- closing it would record a visit that ended, which is not what happened, and
-- the dwell and queue reports would then read it as a real second visit.
-- ---------------------------------------------------------------------------
WITH open_events AS (
    SELECT id, invoice_id, stage_id, entered_at
      FROM tracker_stage_event
     WHERE exited_at IS NULL AND event_type <> 'NOTE'
), dup_pairs AS (
    SELECT invoice_id, stage_id
      FROM open_events
     GROUP BY invoice_id, stage_id
    HAVING count(*) > 1
       AND count(DISTINCT entered_at) = 1     -- same instant => annotation
), annotations AS (
    SELECT max(e.id) AS id                    -- the later-written row
      FROM open_events e
      JOIN dup_pairs p USING (invoice_id, stage_id)
     GROUP BY e.invoice_id, e.stage_id
)
UPDATE tracker_stage_event t
   SET event_type = 'NOTE'
  FROM annotations a
 WHERE t.id = a.id;

-- ---------------------------------------------------------------------------
-- B. Two stale rejections  (invoices 345 and 372, both at stage 5)
--
-- Different shape: a REJECTED visit from 24 Aug that never received its written
-- reason, plus a genuine RE-RECEIVE at the same stage later (26 Aug and 4 Sep).
-- Two real visits, the earlier one never closed.
--
-- The earlier visit is closed at the moment the later one began, which is when
-- it actually ended.
--
-- KNOWN SIDE EFFECT, and the reason this is separated from A: `tracker/views.py`
-- computes `awaiting_remarks = (decision == 'REJECTED' and exited_at is None)`,
-- so these two rows show on the tracker as "awaiting the written reason".
-- Closing them drops them out of that queue. That is the intended outcome --
-- the invoice was re-received weeks ago and the reason was never written -- but
-- it is a visible change, not just a data tidy.
-- ---------------------------------------------------------------------------
WITH open_events AS (
    SELECT id, invoice_id, stage_id, entered_at
      FROM tracker_stage_event
     WHERE exited_at IS NULL AND event_type <> 'NOTE'
), dup_pairs AS (
    SELECT invoice_id, stage_id
      FROM open_events
     GROUP BY invoice_id, stage_id
    HAVING count(*) > 1
       AND count(DISTINCT entered_at) > 1     -- distinct visits
), ranked AS (
    SELECT e.id, e.invoice_id, e.stage_id, e.entered_at,
           lead(e.entered_at) OVER (PARTITION BY e.invoice_id, e.stage_id
                                        ORDER BY e.entered_at) AS next_entered_at
      FROM open_events e
      JOIN dup_pairs p USING (invoice_id, stage_id)
)
UPDATE tracker_stage_event t
   SET exited_at = r.next_entered_at
  FROM ranked r
 WHERE t.id = r.id
   AND r.next_entered_at IS NOT NULL;         -- leaves the newest visit open

COMMIT;

-- Verification: must return 0.
--
--   SELECT count(*) FROM (
--       SELECT invoice_id, stage_id FROM tracker_stage_event
--        WHERE exited_at IS NULL AND event_type <> 'NOTE'
--        GROUP BY 1, 2 HAVING count(*) > 1) d;
