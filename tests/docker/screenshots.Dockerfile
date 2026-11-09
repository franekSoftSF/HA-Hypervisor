FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
RUN pip install --no-cache-dir playwright==1.55.0 requests "pyvmomi>=9.0,<10" PyYAML
