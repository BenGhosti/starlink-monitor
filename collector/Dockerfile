FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN git clone --depth 1 https://github.com/sparky8512/starlink-grpc-tools.git /tmp/sgt \
    && cp /tmp/sgt/starlink_grpc.py . \
    && rm -rf /tmp/sgt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

CMD ["python", "collect.py"]
