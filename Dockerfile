# 1) Use a public, headless IB Gateway image
FROM ghcr.io/gnzsnz/ib-gateway:10.30.1v  # 

# 2) Switch to root to install Python venv support
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

# 3) Create and activate a venv, then install your Python deps
RUN python3 -m venv /venv
ENV PATH="/venv/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4) Copy your bot code
COPY . .

# 5) Launch your bot (gateway is already running in the base image)
CMD ["python", "covered call strategy.py"]
