# container-exporter - the Docker daemon's own facts about every container on
# the box, as Prometheus metrics. See exporter.py for what and why.
#
# stdlib only: no pip, no build step. Runs as nobody; the docker socket is
# readable through the docker group the compose service adds (DOCKER_GID).

FROM python:3.12-alpine

WORKDIR /app
COPY exporter.py /app/exporter.py

ENV EXPORTER_PORT=9417
EXPOSE 9417

USER nobody
CMD ["python", "-u", "exporter.py"]
