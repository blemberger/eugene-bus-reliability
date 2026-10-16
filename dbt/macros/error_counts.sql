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

-- The same as running counts of the signed error, below each of a set of seconds from 15 min
-- early to 15 min late: n_sle_m900s (15 min or more earlier than told), ..., n_sle_0s, ...,
-- n_sle_900s. These give where 8 in 10 buses came against what they were told, early or late,
-- for any period and choice (app/common.py SIGNED_THRESHOLDS lists the same). Kept only on the
-- rows `when` picks (others null), as the site uses them for those alone.
{% macro signed_error_thresholds() %}
    {{ return([-900, -600, -480, -360, -300, -240, -180, -150, -120, -90, -60, -45, -30, -15,
               0, 15, 30, 45, 60, 90, 120, 150, 180, 240, 300, 360, 480, 600, 900]) }}
{% endmacro %}

{% macro signed_error_counts(col, when) %}
    {%- for t in signed_error_thresholds() %}
    case when {{ when }} then count(*) filter (where {{ col }} <= {{ t }}) end
        as n_sle_{{ 'm' ~ (-t) if t < 0 else t }}s{{ "," if not loop.last }}
    {%- endfor %}
{% endmacro %}