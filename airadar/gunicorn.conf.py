# Gunicorn сам підхоплює цей файл із поточного каталогу.
# AI-вердикт може йти до ~45 с (OPENROUTER_DEADLINE_SECONDS), тому timeout воркера більший за дефолтні 30 с.
timeout = 60
