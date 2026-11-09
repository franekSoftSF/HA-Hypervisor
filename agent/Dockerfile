FROM python:3.14-slim

WORKDIR /app
COPY pyproject.toml ./
COPY ha_hypervisor_agent ./ha_hypervisor_agent
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 agent \
    && mkdir -p /config /data \
    && chown agent /data

USER agent
ENV HA_HYPERVISOR_CONFIG=/config/config.yaml
VOLUME ["/data"]
ENTRYPOINT ["ha-hypervisor-agent"]
CMD ["run", "--config", "/config/config.yaml"]
