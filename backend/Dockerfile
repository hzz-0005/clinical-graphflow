FROM python:3.13-slim

WORKDIR /code

COPY pyproject.toml /code/pyproject.toml
COPY app /code/app

# The Temporal control-plane route is configuration-gated, but the API image must contain the
# optional SDK so enabling INSIGHTFLOW_TEMPORAL_ENABLED can actually connect to a cluster.  The
# default setting remains false, so ordinary deployments do not start a Temporal client.
RUN pip install --no-cache-dir ".[dev,runtime,temporal]"

CMD ["fastapi", "run", "app/main.py", "--host", "0.0.0.0", "--port", "8000"]

