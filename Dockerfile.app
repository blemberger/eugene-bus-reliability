# Dashboard image: the same package with the app extras, serving Streamlit on 8501.
FROM python:3.14-slim
WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY eugene_bus_reliability ./eugene_bus_reliability
RUN pip install --no-cache-dir -c constraints.txt ".[app]"
COPY app ./app
# the site's title and description in the HTML Streamlit serves (search engines, link previews)
RUN python app/seo_index.py
COPY .streamlit ./.streamlit
EXPOSE 8501
CMD ["streamlit", "run", "app/streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0"]