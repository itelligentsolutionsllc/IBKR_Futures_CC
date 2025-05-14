# Use a slim Python base
FROM python:3.10-slim

# 1) Install Java & unzip
RUN apt-get update \
 && apt-get install -y --no-install-recommends default-jre-headless wget unzip \
 && rm -rf /var/lib/apt/lists/*

# 2) Download & install IB Gateway silently via CLI .sh
WORKDIR /opt/ibgw
RUN wget https://download.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh \
 && chmod +x ibgateway-stable-standalone-linux-x64.sh \
 && ./ibgateway-stable-standalone-linux-x64.sh -c \
 && rm ibgateway-stable-standalone-linux-x64.sh

# 3) Copy in your bot code & install Python deps
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 4) Copy the entrypoint helper & make it executable
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 5) Kick off gateway + bot
ENTRYPOINT ["/entrypoint.sh"]
CMD ["covered call strategy.py"]
