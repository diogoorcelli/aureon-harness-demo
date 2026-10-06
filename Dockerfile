FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY . .
# Extras opcionais (pgvector): docker build --build-arg EXTRAS=1 .
ARG EXTRAS=0
RUN if [ "$EXTRAS" = "1" ]; then pip install --no-cache-dir -r requirements-extras.txt; fi
CMD ["python", "-m", "adapters.cli"]
