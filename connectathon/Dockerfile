# Pinned base image: a floating "3.12-slim" makes two builds a week apart
# different images. Bump this tag deliberately, not by accident.
FROM python:3.12.14-slim-bookworm

WORKDIR /app

# Dependencies first so the layer caches across app.py edits. Versions are
# pinned in requirements.txt.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
# The backup daemon runs from this same image as its own compose service - it
# opens the SQLite database directly, so it has to be beside it. Stdlib only,
# so it adds nothing to requirements.txt.
COPY backup.py .
# Likewise the practice-period reset daemon, which opens the same database.
COPY reset.py .
# And the keep-warm daemon, which opens nothing - it imports app.py only so
# that the categories it keeps compiled are the categories the service accepts,
# and sends the sample below at the validator on a timer.
COPY warm.py .
# And the subscription notifier, which drains the queue the service fills. The
# service imports it too, for the endpoint check, so it is not optional here.
COPY notify.py .
# Only the bundles: generate.py is the authoring tool, not part of the service.
COPY samples/*.json ./samples/

ENV DATA_DIR=/data
# PORT is honoured by app.py's __main__ entry point, which also binds 0.0.0.0.
ENV PORT=8095
EXPOSE 8095

# Runs as root, which is acceptable for a single-purpose container on a closed
# event network; /data must be writable by whatever user you switch to if you
# add a USER line.
CMD ["python", "app.py"]
