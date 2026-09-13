FROM python:3.11-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py session.py vad.py ari_client.py resample.py config_loader.py ./

EXPOSE 8081
CMD ["python", "main.py"]
