# Demo services image (shop-api, payments, alert-receiver).
# Standard library only: nothing is downloaded except the base image.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root user; /var/log/app is owned by it so the shared volume is writable.
RUN groupadd -g 10001 app \
 && useradd -u 10001 -g app -M -s /usr/sbin/nologin app \
 && mkdir -p /var/log/app \
 && chown app:app /var/log/app

WORKDIR /srv
COPY app.py .
USER app
EXPOSE 8080 9000 5001
CMD ["python", "app.py"]
