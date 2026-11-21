ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.24
FROM $BUILD_FROM

RUN apk add --no-cache tzdata \
 && cp /usr/share/zoneinfo/UTC /etc/localtime \
 && echo "UTC" > /etc/timezone

WORKDIR /app

COPY requirements.txt .
# matplotlib publishes no musllinux wheel for aarch64, so on ARM64 pip builds it from
# source: it needs a C/C++ toolchain at build time (removed in the same layer) and
# libstdc++ at run time. amd64 installs every package from a wheel and skips this.
ARG BUILD_ARCH=amd64
RUN if [ "$BUILD_ARCH" != "amd64" ]; then \
      apk add --no-cache libstdc++ \
      && apk add --no-cache --virtual .build-deps build-base pkgconf; \
    fi \
 && pip install --no-cache-dir -r requirements.txt \
 && if [ "$BUILD_ARCH" != "amd64" ]; then apk del .build-deps; fi

COPY jarvis/ ./jarvis/
COPY ha_integration/ ./ha_integration/

CMD ["python3", "-u", "jarvis/main.py"]
