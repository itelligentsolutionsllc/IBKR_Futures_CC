# 1) Base off a headless IB Gateway image
FROM ghcr.io/gnzsnz/ib-gateway:10.30.1v

# 2) Become root and install python3‑venv
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

# 3) Create a venv at /venv and upgrade pip inside it
RUN python3 -m venv /venv \
 && /venv/bin/python -m pip install --upgrade pip

# 4) Copy in your requirements and install them *inside* the venv
WORKDIR /app
COPY requirements.txt .
RUN /venv/bin/pip install --no-cache-dir -r requirements.txt

# 5) Copy the rest of your bot code
COPY . .

# 6) Run your bot with the venv’s Python (IB Gateway is already running)
CMD ["/venv/bin/python", "covered call strategy.py"]
