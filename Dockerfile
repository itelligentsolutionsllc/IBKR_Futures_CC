# 1) Base off a headless IB Gateway image
FROM ghcr.io/gnzsnz/ib-gateway:10.30.1v

# 2) Become root and install venv + pip
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv python3-pip \
 && rm -rf /var/lib/apt/lists/*

# 3) Create & bootstrap a virtualenv at /venv
RUN python3 -m venv /venv \
 && /venv/bin/python -m pip install --upgrade pip

# 4) Ensure the venv's python & pip are used
ENV PATH="/venv/bin:${PATH}"

# 5) Copy in your bot and install its requirements INSIDE the venv
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 6) Finally, launch your bot (IB Gateway is already running for you in the base image)
CMD ["python", "covered call strategy.py"]
