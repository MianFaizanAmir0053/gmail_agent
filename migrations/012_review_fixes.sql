-- M17's review fixes (tasks 17.15-17.18). Additive, and can be re-run.

-- A model in use with no price stops new work as the cap does (D5): the
-- watch records it, and the web app's header says so.
ALTER TABLE control DROP CONSTRAINT IF EXISTS control_budget_state_check;
ALTER TABLE control ADD CONSTRAINT control_budget_state_check
    CHECK (budget_state IN ('ok', 'warning', 'exhausted', 'unpriced'));

-- The timeline asks, per card, whether a withdraw was declined, and the
-- watch counts unconfirmed writes: both look rows up by decision.
CREATE INDEX IF NOT EXISTS audit_log_decision_idx ON audit_log (decision_id, kind);
