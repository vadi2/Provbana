FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi uvicorn
WORKDIR /app
COPY app.py .
COPY samples ./samples
ENV DATA_DIR=/data
EXPOSE 8095
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8095"]
