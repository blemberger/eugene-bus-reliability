FROM python:3.14-slim
WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY ltdwatch ./ltdwatch
RUN pip install --no-cache-dir -c constraints.txt ".[pipeline]"
COPY dbt ./dbt
COPY sql ./sql
RUN DBT_PROFILES_DIR=/app/dbt POSTGRES_USER=x POSTGRES_PASSWORD=x POSTGRES_DB=x dbt deps --project-dir /app/dbt
CMD ["python", "-m", "ltdwatch", "poll"]