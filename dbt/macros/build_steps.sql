{#
  How long each step of a build took (models and tests), kept for two weeks in
  analytics.build_step_log, so the dump can show where the build's time goes.
#}
{% macro log_build_steps(results) %}
    {%- if flags.WHICH in ('build', 'run') and results -%}
        insert into analytics.build_step_log (invocation_id, node, kind, seconds, status) values
        {%- for r in results %}
            ('{{ invocation_id }}', '{{ r.node.name | replace("'", "") }}', '{{ r.node.resource_type }}',
             {{ r.execution_time or 0 }}, '{{ r.status }}'){{ "," if not loop.last }}
        {%- endfor %};
        delete from analytics.build_step_log where logged_at < now() - interval '14 days'
    {%- else -%}
        select 1
    {%- endif -%}
{% endmacro %}