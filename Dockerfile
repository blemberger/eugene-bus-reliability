# Pipeline image: the poller and the scheduler (dbt) run from it.
FROM python:3.14-slim
WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY eugene_bus_reliability ./eugene_bus_reliability
RUN pip install --no-cache-dir -c constraints.txt ".[pipeline]"
COPY dbt ./dbt
COPY sql ./sql
RUN DBT_PROFILES_DIR=/app/dbt POSTGRES_USER=x POSTGRES_PASSWORD=x POSTGRES_DB=x dbt deps --project-dir /app/dbt
CMD ["python", "-m", "eugene_bus_reliability", "poll"]