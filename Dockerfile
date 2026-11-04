FROM python:3.12-slim

# potrace: dependencia de sistema del pipeline
RUN apt-get update \
 && apt-get install -y --no-install-recommends potrace \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# pre-descargar u2net (~170MB) en la imagen -> primer request instantáneo
RUN python -c "from rembg import new_session; new_session('u2net')"

COPY backend ./backend
COPY frontend ./frontend

EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
