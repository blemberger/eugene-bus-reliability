-- Shared by the honest-countdown mart and the app (app/common.py mirrors these bands).
{% macro horizon_band(col) -%}
    case when {{ col }} <= 1 then 1 when {{ col }} <= 3 then 3 when {{ col }} <= 6 then 6
         when {{ col }} <= 10 then 10 when {{ col }} <= 15 then 15 when {{ col }} <= 20 then 20
         else 30 end
{%- endmacro %}

{% macro day_part(hour_col) -%}
    case when {{ hour_col }} < 9 then 'morning' when {{ hour_col }} < 15 then 'midday'
         when {{ hour_col }} < 19 then 'afternoon' else 'evening' end
{%- endmacro %}