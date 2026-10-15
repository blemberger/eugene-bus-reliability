-- How many arrivals were within each of a set of minutes off (seconds, early or late alike), as
-- running counts: n_le_15s, n_le_30s, ... Unlike percentiles, these add up across days, routes
-- and stops, so the site can find "8 in 10 buses were between x and y minutes off" for any
-- period and choice (app/common.py ERROR_THRESHOLDS lists the same thresholds).
{% macro abs_error_thresholds() %}
    {{ return([15, 30, 45, 60, 90, 120, 150, 180, 240, 300, 360, 480, 600, 900]) }}
{% endmacro %}

{% macro abs_error_counts(col) %}
    {%- for t in abs_error_thresholds() %}
    count(*) filter (where abs({{ col }}) <= {{ t }}) as n_le_{{ t }}s{{ "," if not loop.last }}
    {%- endfor %}
{% endmacro %}