-- mart_accuracy_by_hour and mart_told_vs_actual were replaced by mart_prediction_daily (the
-- same numbers per service day, so the Predictions page can follow the period and days
-- filters). dbt doesn't remove tables of models that no longer exist, so this does. Safe to run
-- more than once.
DROP TABLE IF EXISTS marts.mart_accuracy_by_hour;
DROP TABLE IF EXISTS marts.mart_told_vs_actual;