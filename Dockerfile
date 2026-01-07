FROM python:3.10
WORKDIR /app
COPY . .

# Install all dependencies including pandas
RUN pip install -r requirements.txt

CMD ["uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8000"]
