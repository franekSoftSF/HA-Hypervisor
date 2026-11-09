# The tests run on the real Home Assistant image: the pytest plugin usually
# lags one release behind, and this project targets the newest release.
FROM ghcr.io/home-assistant/home-assistant:stable
COPY plugin_requirements.py /tmp/plugin_requirements.py
RUN uv pip install --no-deps pytest-homeassistant-custom-component \
    && python3 /tmp/plugin_requirements.py > /tmp/requirements.txt \
    && uv pip install --index-strategy unsafe-best-match -r /tmp/requirements.txt

# Plugin built for 2026.10 on Home Assistant 2026.11: a private helper it uses is gone.
RUN P=/usr/local/lib/python3.14/site-packages/pytest_homeassistant_custom_component/common.py \
    && sed -i '/^    _clear_logger_overwrites,$/d' "$P" \
    && sed -i 's/^\( *\)_clear_logger_overwrites(hass)$/\1pass/' "$P"
ENTRYPOINT []
