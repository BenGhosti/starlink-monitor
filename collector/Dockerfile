FROM python:3.12-slim

# iputils-ping fuer ping_watchdog.py, git fuer den Clone von starlink-grpc-tools
# (kein PyPI-Paket - muss aus dem Repo kommen, siehe RUN git clone unten)
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# starlink-grpc-tools (sparky8512) liefert starlink_grpc.py, das wir importieren.
# Es ist kein PyPI-Paket, daher Clone statt pip install. Nur die eine Datei wird
# uebernommen statt --depth 1 clone im /app zu belassen, um die History/restliche
# Tools (die wir nicht brauchen) nicht ins Image zu ziehen.
RUN git clone --depth 1 https://github.com/sparky8512/starlink-grpc-tools.git /tmp/sgt \
    && cp /tmp/sgt/starlink_grpc.py . \
    && rm -rf /tmp/sgt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Eigener Code zuletzt, damit er starlink_grpc.py nicht versehentlich ueberschreibt
COPY . .

RUN mkdir -p /data

CMD ["python", "collect.py"]
