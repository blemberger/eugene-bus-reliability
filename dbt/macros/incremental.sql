{#
  The heavy models are built incrementally, one service day at a time: a build recomputes only
  the latest two service days already in the table (today's, still filling in, and yesterday's,
  which can still get late reports until the 3 am rollover) plus any newer ones, and keeps the
  rest. A finished day doesn't change, so recomputing it every 15 minutes was wasted work.

  The first build, and any build after the analysis code changes (the scheduler checks, see
  eugene_bus_reliability/scheduler.py), is a full refresh over the lookback window instead.

  recent_start()    ->  the first service date this build computes.
  recent_days(col)  ->  a SQL condition on col, the service date column of the model's input.
#}
{% macro recent_start() -%}
    {%- if is_incremental() -%}
        (select coalesce(max(service_date), date '2000-01-01') - 1 from {{ this }})
    {%- else -%}
        (current_date - {{ var('lookback_days') }})
    {%- endif -%}
{%- endmacro %}

{% macro recent_days(col='service_date') -%}
    {{ col }} >= {{ recent_start() }}
{%- endmacro %}