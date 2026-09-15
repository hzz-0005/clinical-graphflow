FROM python:3.13-slim

WORKDIR /usr/app

RUN pip install --no-cache-dir "dbt-postgres>=1.10,<2"

ENTRYPOINT ["dbt"]
CMD ["build", "--profiles-dir", "."]

