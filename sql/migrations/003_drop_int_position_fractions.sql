-- int_position_fractions was replaced by int_positions_along_shape (built incrementally, with
-- buses walked along the route shape instead of snapped to its nearest point). dbt doesn't
-- remove tables of models that no longer exist, so this does. Safe to run more than once.
DROP TABLE IF EXISTS intermediate.int_position_fractions;