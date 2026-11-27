import argparse
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from assistant_engine import Reply, analyze, docs_for, source_excerpt
from analytics import stock_cover
from sheets_sync import ensure_fresh
from store import QuestionError, retrieve, seed, today


HELP = """Я могу посчитать показатели магазина и найти ответ в его документах.
Попробуйте: «Сравни выручку по SKU за две последние полные недели». Затем: «А где выросли расходы на рекламу?»
Или спросите: «Как у нас принято считать запас в днях? Какие товары пора заказать по этому правилу?»

/sales · продажи за вчера
/purchases · закупки за вчера
/stock · остатки на вчера
/cover · на сколько дней хватит запасов
/find текст · найти выдержки из документов
/sources · показать основания последнего ответа
/reset · начать новый диалог"""
COMMANDS = {"/sales": "sales", "/purchases": "purchases", "/stock": "stock"}


@dataclass
class Conversation:
    turns: deque = field(default_factory=lambda: deque(maxlen=4))
    sources: list = field(default_factory=list)


def post(url, payload, headers=None):
    req = Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json", **(headers or {})})
    with urlopen(req, timeout=50) as response:
        return json.load(response)


def model(messages, structured=False):
    payload = {"model": os.environ["LLM_MODEL"], "messages": messages}
    if structured:
        payload["response_format"] = {"type": "json_object"}
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    reply = post(base + "/chat/completions", payload, {"Authorization": "Bearer " + os.environ["LLM_API_KEY"]})
    content = reply["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip() or len(content) > 20000:
        raise ValueError("Пустой или слишком длинный ответ модели.")
    return content


def answer(question, session=None):
    session = session if session is not None else Conversation()
    question = question.strip()
    if question in {"/start", "/help"}:
        return HELP
    if question == "/reset":
        session.turns.clear()
        session.sources.clear()
        return "История этого диалога очищена."
    if question == "/sources":
        return "\n\n".join(session.sources) or "В этом диалоге пока нет сохранённых источников."
    session.sources.clear()
    if question == "/cover":
        ensure_fresh()
        from analytics import catalog
        if "stock" not in catalog()["columns"]:
            return "В этом источнике нет остатков для расчёта запаса."
        report = stock_cover()
        reply = Reply(report, [report, "Расчёт: остаток на конец последнего дня × 14 / продажи в штуках за 14 дней."])
    elif question in COMMANDS:
        ensure_fresh()
        yesterday = (today() - timedelta(days=1)).isoformat()
        from analytics import catalog, run_query
        columns = catalog()["columns"]
        if question == "/sales" and "revenue_rub" not in columns:
            result = run_query(f"SELECT SUM(sold) FROM daily WHERE day='{yesterday}'")
            sold = result["rows"][0][0]
            if sold is None:
                return f"В источнике нет строк за {yesterday}."
            report = f"Продажи за {yesterday}: {sold} шт.\nИсточник: {result['source']} · {result['updated_at']}"
            session.sources = [report]
            session.turns.append((question, report))
            return report
        if question == "/stock" and "stock" not in columns or question == "/purchases" and "purchase_rub" not in columns:
            return "Этот показатель отсутствует в подключённом источнике."
        facts = retrieve(COMMANDS[question], yesterday, yesterday, None)
        reply = Reply(facts, [facts])
    elif question == "/find":
        return "Напишите /find и тему, например /find срок пополнения."
    elif question.startswith("/find "):
        hits = docs_for(question[6:])
        if not hits:
            return "В подключённых документах подходящих фрагментов не нашлось."
        extracts = [source_excerpt(hit) for hit in hits]
        reply = Reply("\n\n".join(extracts), extracts)
    elif not os.getenv("LLM_API_KEY") or not os.getenv("LLM_MODEL"):
        return "Для свободных вопросов настройте LLM_API_KEY и LLM_MODEL. Без ключа работают /find, /sales, /purchases и /stock."
    else:
        reply = analyze(question, list(session.turns), model)
    session.sources = reply.sources
    session.turns.append((question[:1500], reply.text[:1500]))
    return reply.text


def safe_answer(text, session=None):
    try:
        return answer(text, session)
    except QuestionError as error:
        return str(error)
    except Exception as error:
        print("Ошибка обработки:", type(error).__name__, flush=True)
        return "Не удалось получить ответ. Проверьте доступность источника и API. Диагностика доступна через python bot.py check."


def telegram():
    base = "https://api.telegram.org/bot" + os.environ["TELEGRAM_BOT_TOKEN"] + "/"
    allowed = {int(v.strip()) for v in os.environ["TELEGRAM_USER_IDS"].split(",") if v.strip()}
    if not allowed:
        raise SystemExit("Укажите числовые Telegram ID в TELEGRAM_USER_IDS.")
    sessions = {}
    offset = 0
    while True:
        try:
            updates = post(base + "getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]})
            for update in updates["result"]:
                msg = update.get("message", {})
                user_id = msg.get("from", {}).get("id")
                if (msg.get("chat", {}).get("type") == "private" and user_id in allowed
                        and isinstance(msg.get("text"), str)):
                    result = safe_answer(msg["text"][:2000], sessions.setdefault(user_id, Conversation()))
                    for i in range(0, len(result), 2000):
                        try:
                            post(base + "sendMessage", {"chat_id": msg["chat"]["id"], "text": result[i:i+2000]})
                        except HTTPError as error:
                            if error.code == 403:
                                break
                            raise
                offset = update["update_id"] + 1
        except HTTPError as error:
            if error.code in (401, 404, 409):
                raise SystemExit(f"Telegram HTTP {error.code}. Проверьте токен и отсутствие второго получателя обновлений.")
            delay = 5
            if error.code == 429:
                try:
                    delay = max(1, int(json.load(error)["parameters"]["retry_after"]))
                except (KeyError, ValueError, TypeError):
                    pass
            time.sleep(delay)
        except (URLError, TimeoutError, ConnectionError):
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["demo", "ask", "chat", "check", "telegram"])
    parser.add_argument("question", nargs="?", default="/sales")
    args = parser.parse_args()
    if not os.getenv("SELLER_DB") and not os.getenv("SELLER_SHEET_ID"):
        seed()
    if args.mode == "telegram":
        telegram()
    elif args.mode == "check":
        from analytics import catalog
        ensure_fresh()
        print("Данные:", json.dumps(catalog(), ensure_ascii=False, default=str))
        print("Свободные вопросы:", "настроены ключ и модель" if os.getenv("LLM_MODEL") and os.getenv("LLM_API_KEY") else "нужны LLM_MODEL и LLM_API_KEY")
        print("Telegram:", "заданы параметры" if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_USER_IDS") else "нужны TELEGRAM_BOT_TOKEN и TELEGRAM_USER_IDS")
    elif args.mode == "chat":
        session = Conversation()
        print(HELP)
        while True:
            try:
                q = input("\nВы > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q == "/exit":
                break
            if q:
                print(safe_answer(q, session))
    else:
        print(safe_answer(args.question))


if __name__ == "__main__":
    main()
