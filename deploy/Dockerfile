FROM python:3.11-slim

ENV TZ=Europe/Prague
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY odyssey_watch.py .

ENV STATE_FILE=/data/seen_events.json
ENV HORIZON_LOG=/data/horizon.csv

CMD ["python3", "odyssey_watch.py", "--loop"]
