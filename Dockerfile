FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV PT_DB_PATH=/data/paper_trader.db
VOLUME /data
EXPOSE 8000
# One worker on purpose: the quote cache and snapshot loop live in-process.
CMD ["uvicorn", "app.main:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
