FROM python:3.14-slim
RUN pip install --no-cache-dir "paho-mqtt>=2.1,<3" "pyvmomi>=9.0,<10" "pyghmi>=1.6,<2" "PyYAML>=6,<7" "requests>=2.32,<3" pytest
