FROM python:3.12-slim

# PYTHONUNBUFFERED: логи сразу попадают в `docker compose logs`, а не копятся в буфере.
# PYTHONDONTWRITEBYTECODE: не создаём .pyc внутри образа.
# HF_HOME: кэш моделей лежит в образе. HF_HUB_OFFLINE=1: во время работы ничего не скачиваем,
# поэтому первый вопрос не ждёт сети, а запуск не зависит от Hugging Face.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf-cache \
    HF_HUB_OFFLINE=1

WORKDIR /app

# Обычный пользователь вместо root: если в боте найдут уязвимость, ущерб будет меньше.
RUN useradd --create-home --uid 1000 app

# CPU-версия torch (без CUDA) делает образ в разы меньше. Ставим её первой: тогда pip,
# увидев torch из requirements.txt (через sentence-transformers), считает его установленным.
RUN pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu

# Сначала ТОЛЬКО requirements.txt и установка зависимостей. Docker кэширует слои:
# пока список зависимостей не менялся, правки кода не вызывают долгую переустановку.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Модель эмбеддингов скачивается при сборке. Значение должно совпадать с EMBEDDING_MODEL
# в .env: compose передаёт его сюда как build-аргумент.
ARG EMBEDDING_MODEL=intfloat/multilingual-e5-base
RUN HF_HUB_OFFLINE=0 python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')" \
    && chown -R app:app /opt/hf-cache

COPY --chown=app:app . .
USER app

CMD ["python", "bot.py"]
