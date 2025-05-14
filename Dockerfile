FROM python:3.10-slim

# 1) Install Java & tools
RUN apt-get update \
 && apt-get install -y default-jre-headless wget unzip

# 2) Download & install IB Gateway silently
WORKDIR /ibgw
RUN wget https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh \
 && chmod +x ibgateway-stable-standalone-linux-x64.sh \
 && ./ibgateway-stable-standalone-linux-x64.sh --silent

# 3) Copy your bot
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 4) Launch gateway in background, then bot
CMD bash -lc "\
    /ibgw/ibgatewaystart.sh --nogui & \
    sleep 15 && \
    python 'covered call strategy.py' \
"
