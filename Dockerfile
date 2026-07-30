FROM node:22-bookworm-slim AS ui-build

WORKDIR /build/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

FROM python:3.11-slim-bullseye

WORKDIR /app

COPY ./requirements.txt requirements.txt
RUN pip3 install --no-cache-dir --upgrade -r requirements.txt
RUN mkdir /config

ADD . .
COPY --from=ui-build /build/intg-manager/static/app ./intg-manager/static/app

# Network configuration
ENV UC_DISABLE_MDNS_PUBLISH="false"
ENV UC_MDNS_LOCAL_HOSTNAME=""
ENV UC_INTEGRATION_INTERFACE="0.0.0.0"
ENV UC_INTEGRATION_HTTP_PORT="9090"

# Configuration path
ENV UC_CONFIG_HOME="/config"

LABEL org.opencontainers.image.source="https://github.com/JackJPowell/uc-intg-manager"

CMD ["python3", "-u", "intg-manager/driver.py"]
