# AkiDoDo Backend

FastAPI backend для публичной доски AkiDoDo.

Он:

- принимает публикации от Telegram-бота владельца;
- отдаёт публичный список постов через `GET /posts`;
- передаёт сообщения посетителей из публичного Telegram-бота владельцу;
- хранит данные в SQLite-файле `backend/akidodo_posts.db`.

## Запуск

```bash
python -m pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 3000
```

Документация API:

```text
http://localhost:8000/docs
```
