-- Adds the BRIN index on rt.prediction_history(closed_at) that sql/schema/020_rt.sql now
-- creates, to databases built before it. Safe to run more than once.
CREATE INDEX IF NOT EXISTS prediction_history_closed_at_idx
    ON rt.prediction_history USING brin (closed_at);