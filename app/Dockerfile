FROM python:3.12-slim

WORKDIR /app

RUN mkdir -p /var/log/flask-app && \
    addgroup --system flask && \
    adduser --system --ingroup flask flask && \
    chown flask:flask /var/log/flask-app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

USER flask

EXPOSE 5000

CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:5000", \
     "--log-file", "/var/log/flask-app/gunicorn.log", \
     "--access-logfile", "-", \
     "app:app"]
