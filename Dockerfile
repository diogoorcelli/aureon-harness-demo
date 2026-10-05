FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY . .
CMD ["python", "-m", "adapters.cli"]
