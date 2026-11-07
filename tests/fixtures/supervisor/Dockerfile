ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
USER root
RUN python3 -m pip install --no-cache-dir --target /opt/devdash-supervisor supervisor==4.3.0
ENV PYTHONPATH=/opt/devdash-supervisor
COPY supervisord.conf /etc/devdash-supervisord.conf
COPY worker.py /opt/devdash-worker.py
HEALTHCHECK --interval=1s --timeout=1s --start-period=1s --retries=3 CMD ["python3", "-c", "import os,sys; sys.exit(0 if os.path.exists('/tmp/devdash-healthy') else 1)"]
ENTRYPOINT ["python3", "-m", "supervisor.supervisord", "-n", "-c", "/etc/devdash-supervisord.conf"]
