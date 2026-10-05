{#
  Summary tables over all history (medians and percentiles, which can't be built up a day at
  a time) are recomputed at most once an hour instead of at every 15-minute build: an hour of
  new data barely moves numbers over days or weeks, and these were most of an ordinary build's
  time. A full refresh (first build, analysis code changed, `make server-full-refresh`) always
  recomputes them.

  Use: config(**hourly_config()) and wrap the model's query with hourly_start() / hourly_end().
  Every row gets refresh_key = 1 and built_at; when the table is less than 55 minutes old the
  query returns no rows, so nothing is deleted or added and Postgres skips the work entirely.
#}
{% macro hourly_config() %}
    {{ return({'materialized': 'incremental', 'incremental_strategy': 'delete+insert', 'unique_key': 'refresh_key'}) }}
{% endmacro %}

{% macro hourly_start() -%}
select 1 as refresh_key, now() as built_at, hourly_rows.* from (
{%- endmacro %}

{% macro hourly_end() -%}
) hourly_rows
{%- if is_incremental() %}
where (select max(built_at) from {{ this }}) < now() - interval '55 minutes'
{%- endif %}
{%- endmacro %}