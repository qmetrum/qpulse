FROM python:3.11-slim

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: run the latency benchmark. Override with `command:` in compose
# or `docker run ... python -m app.live` once a live entrypoint exists.
CMD ["python", "-m", "app.bench"]
