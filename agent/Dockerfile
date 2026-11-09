FROM python:3.14-slim

WORKDIR /app
COPY pyproject.toml ./
COPY ha_vmware_agent ./ha_vmware_agent
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 agent \
    && mkdir -p /config /data \
    && chown agent /data

USER agent
VOLUME ["/data"]
ENTRYPOINT ["ha-vmware-agent"]
CMD ["run", "--config", "/config/config.yaml"]
