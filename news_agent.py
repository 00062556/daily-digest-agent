"""
Агент «Дайджест для разработчика»: читает новости об ИИ, Java/Spring
и инструментах разработки (IntelliJ IDEA, JetBrains, GitHub), выбирает главное,
пишет краткую сводку на русском и публикует её в Telegram-канал.

Устроен как простой агент из agent.py:
  LLM (выбирает и пишет) + инструменты (читают RSS, статьи, публикуют) + цикл.

Переменные окружения:
  OPENAI_API_KEY      ключ OpenAI
  TELEGRAM_BOT_TOKEN  токен бота от @BotFather
  TELEGRAM_CHAT_ID    @имя_канала или числовой id (-100...)
  DRY_RUN=1           не публиковать, а только напечатать дайджест (для отладки)
  MODEL               модель (по умолчанию gpt-5-mini)
  HOURS               за сколько часов брать новости (по умолчанию 24)

Запуск:  python news_agent.py
"""

import calendar
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests
from openai import OpenAI

MODEL = os.getenv("MODEL") or "gpt-5-mini"
HOURS = int(os.getenv("HOURS", "24"))
DRY_RUN = os.getenv("DRY_RUN") == "1"
TZ = ZoneInfo("Asia/Almaty")
HEADERS = {"User-Agent": "Mozilla/5.0 (news-digest-agent)"}

# Источники по разделам. Добавить ленту = одна строка, новый раздел = новый ключ.
SOURCES = {
    "ai": [
        ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("OpenAI", "https://openai.com/news/rss.xml"),
        ("Google AI", "https://blog.google/technology/ai/rss/"),
        ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
        ("Хабр: ИИ", "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/"),
    ],
    "java": [
        ("Inside Java", "https://inside.java/feed.xml"),
        ("InfoQ Java", "https://feed.infoq.com/java"),
        ("Spring", "https://spring.io/blog.atom"),
        ("Хабр: Java", "https://habr.com/ru/rss/hubs/java/articles/"),
    ],
    "tools": [
        ("IntelliJ IDEA Blog", "https://blog.jetbrains.com/idea/feed/"),
        ("JetBrains Blog", "https://blog.jetbrains.com/feed/"),
        ("GitHub Blog", "https://github.blog/feed/"),
        ("Hacker News (150+ баллов)", "https://hnrss.org/frontpage?points=150"),
    ],
}
SECTIONS = {"ai": "ИИ и нейросети", "java": "Java и Spring", "tools": "IDE и инструменты разработки"}
PER_SOURCE = 15   # не больше N свежих новостей с одной ленты

client = OpenAI()
seen_links: set[str] = set()   # статьи, которые агент видел в заголовках
published = False              # публикуем строго один раз за запуск


def annotate(level: str, message: str) -> None:
    """В GitHub Actions строка ::error::текст показывается в аннотациях запуска."""
    if os.getenv("GITHUB_ACTIONS"):
        msg = str(message).replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::{level}::{msg[:1500]}")


def clean(text: str, limit: int) -> str:
    """Убирает HTML-теги и лишние пробелы, обрезает до limit символов."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


# ─────────────────────────────────────────────────────────────
# ИНСТРУМЕНТЫ
# ─────────────────────────────────────────────────────────────

def get_headlines(section: str) -> dict:
    """Свежие заголовки из RSS раздела. Фильтр по времени делает код, не LLM."""
    if section not in SOURCES:
        return {"error": f"Неизвестный раздел {section}. Доступны: {list(SOURCES)}"}
    cutoff = time.time() - HOURS * 3600
    items, errors = [], []
    for name, url in SOURCES[section]:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        for entry in feed.entries[:PER_SOURCE]:
            ts = entry.get("published_parsed") or entry.get("updated_parsed")
            if ts and calendar.timegm(ts) < cutoff:  # feedparser отдаёт время в UTC
                continue
            link = entry.get("link", "")
            seen_links.add(link)
            items.append({
                "source": name,
                "title": clean(entry.get("title", ""), 200),
                "summary": clean(entry.get("summary", ""), 300),
                "link": link,
            })
    return {"section": section, "count": len(items), "items": items, "errors": errors}


def read_article(url: str) -> dict:
    """Полный текст статьи. Только по ссылкам из get_headlines: агент не ходит куда попало."""
    if url not in seen_links:
        return {"error": "Можно читать только статьи из полученных заголовков"}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        return {"error": str(e)}
    body = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", resp.text)
    return {"url": url, "text": clean(body, 5000)}


def publish_digest(text: str) -> dict:
    """Публикует готовый дайджест в Telegram (или печатает при DRY_RUN=1)."""
    global published
    if published:
        return {"error": "Дайджест уже опубликован в этом запуске"}

    if DRY_RUN:
        print("\n" + "=" * 60 + "\nDRY_RUN: дайджест не отправлен\n" + "=" * 60)
        print(text)
        published = True
        return {"status": "dry_run_ok"}

    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    api = f"https://api.telegram.org/bot{token}/sendMessage"

    # Лимит Telegram — 4096 символов на сообщение, режем по абзацам
    chunks, current = [], ""
    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 > 4000:
            chunks.append(current)
            current = ""
        current += para + "\n\n"
    chunks.append(current)

    for chunk in chunks:
        data = {"chat_id": chat_id, "text": chunk.strip(),
                "parse_mode": "HTML", "disable_web_page_preview": True}
        r = requests.post(api, data=data, timeout=20)
        if r.status_code == 400:  # битая HTML-разметка: отправляем простым текстом
            data.pop("parse_mode")
            plain = re.sub(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', r"\2: \1", chunk)
            data["text"] = html.unescape(re.sub(r"</?(b|i|u|s|code)>", "", plain)).strip()[:4096]
            r = requests.post(api, data=data, timeout=20)
        if not r.ok:
            return {"status": "error", "telegram": r.text}
    published = True
    return {"status": "sent", "messages": len(chunks)}


TOOLS = [
    {
        "name": "get_headlines",
        "description": f"Заголовки новостей за последние {HOURS} ч. из RSS по разделу. "
                       + ", ".join(f"'{k}' — {v}" for k, v in SECTIONS.items()) + ".",
        "parameters": {"type": "object",
                         "properties": {"section": {"type": "string", "enum": list(SOURCES)}},
                         "required": ["section"]},
    },
    {
        "name": "read_article",
        "description": "Полный текст статьи по ссылке из get_headlines. Используй, только если "
                       "заголовка и анонса мало, чтобы понять суть. Не больше 5 статей.",
        "parameters": {"type": "object",
                         "properties": {"url": {"type": "string"}},
                         "required": ["url"]},
    },
    {
        "name": "publish_digest",
        "description": "Публикует готовый дайджест в Telegram-канал. Вызывается один раз, в конце.",
        "parameters": {"type": "object",
                         "properties": {"text": {"type": "string", "description": "Текст в Telegram HTML"}},
                         "required": ["text"]},
    },
]
FUNCTIONS = {"get_headlines": get_headlines, "read_article": read_article,
             "publish_digest": publish_digest}


SYSTEM = """Ты — редактор ежедневного Telegram-дайджеста для Java-разработчика,
которому интересны искусственный интеллект и инструменты разработки.

Порядок работы:
1. Получи заголовки по всем разделам: ai, java, tools.
2. Выбери главное: 3–6 новостей на раздел. Если в разделе нет ничего стоящего, пропусти его.
   Приоритет:
   - ИИ: новые модели и их возможности, ИИ-ассистенты для кода и агенты, открытые модели,
     крупные события индустрии, практические статьи для разработчиков;
   - Java: релизы JDK и JEP, Spring и Spring AI, Kotlin и JVM, производительность, безопасность;
   - инструменты: релизы и новые функции IntelliJ IDEA и других IDE JetBrains, GitHub и Copilot,
     заметные open-source-проекты.
   Пропускай маркетинг, истории клиентов, вакансии, вебинары и повторы.
   Одно событие из нескольких источников — одна новость.
3. Если по заголовку непонятна суть, прочитай статью через read_article.
4. Напиши дайджест на русском и опубликуй через publish_digest.

Формат (Telegram HTML, разрешены только <b>, <i>, <a href="...">):
<b>Дайджест разработчика — {date}</b>

<b>ИИ и нейросети</b>
• <b>Короткий заголовок.</b> Одно-два предложения: что произошло и чем это полезно разработчику. <a href="ссылка">Источник</a>

<b>Java и Spring</b>
• ...

<b>IDE и инструменты разработки</b>
• ...

Правила:
- Только факты из полученных новостей, ничего не додумывай.
- Каждая новость со ссылкой на источник.
- Англоязычные новости переводи на русский; названия продуктов и технологий оставляй как есть.
- Символы <, >, & в тексте заменяй на &lt; &gt; &amp;.
- Весь дайджест до 3800 символов."""


# ─────────────────────────────────────────────────────────────
# ЦИКЛ АГЕНТА
# ─────────────────────────────────────────────────────────────

def run(max_steps: int = 15) -> None:
    today = datetime.now(TZ).strftime("%d.%m.%Y")
    messages = [
        {"role": "system", "content": SYSTEM.replace("{date}", today)},
        {"role": "user", "content": f"Подготовь и опубликуй дайджест разработчика за {today}."},
    ]
    tools = [{"type": "function", "function": t} for t in TOOLS]

    for step in range(1, max_steps + 1):
        response = client.chat.completions.create(model=MODEL, messages=messages, tools=tools,
                                                  max_completion_tokens=16000)
        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:          # инструменты больше не нужны: агент закончил
            break

        for call in msg.tool_calls:     # модель попросила вызвать функции
            args = json.loads(call.function.arguments or "{}")
            print(f"[Шаг {step}] {call.function.name}({json.dumps(args, ensure_ascii=False)[:100]})")
            output = FUNCTIONS[call.function.name](**args)
            if output.get("error") or output.get("errors") or output.get("status") == "error":
                problem = f"{call.function.name}: {json.dumps(output, ensure_ascii=False)[:500]}"
                print(f"          Проблема: {problem}")
                annotate("warning", problem)
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(output, ensure_ascii=False)})

    if not published:
        last = messages[-1].get("content") if isinstance(messages[-1], dict) else None
        print(f"Последний ответ модели: {last}", file=sys.stderr)
        print("Агент завершил работу, но дайджест не опубликован.", file=sys.stderr)
        annotate("error", f"Дайджест не опубликован (шагов: {step}). Последний ответ модели: {last}")
        sys.exit(1)  # чтобы запуск по расписанию отметился как упавший
    print(f"Готово: {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        annotate("error", f"{type(e).__name__}: {e}")
        raise
