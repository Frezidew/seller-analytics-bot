import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import assistant_engine
import bot
import knowledge
import store


STOCK_SQL = """WITH latest AS (SELECT MAX(day) AS d FROM daily),
period AS (SELECT sku, day, stock, sold, d FROM daily CROSS JOIN latest
 WHERE day BETWEEN date(d, '-13 days') AND d)
SELECT sku, d, COUNT(*) AS days, SUM(sold) AS sold_14,
 MAX(CASE WHEN day=d THEN stock END) AS stock_end,
 ROUND(MAX(CASE WHEN day=d THEN stock END)*14.0/NULLIF(SUM(sold),0),2) AS days_left,
 SUM(CASE WHEN stock=0 THEN 1 ELSE 0 END) AS zero_days
FROM period GROUP BY sku,d HAVING COUNT(*)=14 ORDER BY days_left"""


class PortfolioFlow(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "demo.sqlite3"
        self.enterContext(patch.object(store, "DEMO", db))
        self.enterContext(patch.dict(os.environ, {"LLM_API_KEY": "fixture", "LLM_MODEL": "fixture"}, clear=True))
        store.seed()

    def test_document_to_sql_and_audit(self):
        found = {}
        def fixture(messages, structured):
            self.assertTrue(structured)
            if len(messages) == 2:
                return json.dumps({"action":"search","query":"порог пополнения запас 14 дней"})
            observed = json.loads(messages[-1]["content"])["evidence"]
            if len(messages) == 4:
                key = next(k for k,h in observed.items() if "меньше чем на 7 дней" in h["text"])
                found["doc"] = key
                return json.dumps({"action":"sql","sql": STOCK_SQL})
            key, data = next(iter(observed.items()))
            self.assertEqual(data["columns"][-2:], ["days_left","zero_days"])
            with closing(sqlite3.connect(store.DEMO)) as db:
                for sku, last, days, sold, stock, left, zero in data["rows"]:
                    rows = db.execute("SELECT sold,stock FROM daily WHERE sku=? ORDER BY day DESC LIMIT 14",(sku,)).fetchall()
                    self.assertEqual(days, 14)
                    self.assertAlmostEqual(left, rows[0][1]*14/sum(r[0] for r in rows), places=2)
                    self.assertEqual(zero, sum(r[1]==0 for r in rows))
            self.assertEqual(len(data["rows"]), 3)
            return json.dumps({"action":"answer","refs":[found["doc"],key],"text":"Проверка закончена."})
        session = bot.Conversation()
        with patch.object(bot, "model", fixture):
            text = bot.answer("Какой запас пора пополнить по нашему правилу?", session)
        self.assertIn("inventory.md", text)
        self.assertIn("daily", text)
        self.assertIn("SELECT", bot.answer("/sources",session))
        self.assertNotIn("SELECT",bot.answer("/sources", bot.Conversation()))
        followup = []
        def clarify(messages, structured):
            followup.extend(json.loads(messages[1]["content"])["history"])
            return '{"action":"clarify","text":"За какой период?"}'
        with patch.object(bot,"model",clarify):
            bot.answer("А если взять прошлую неделю?",session)
        self.assertEqual(len(followup),1)
        bot.answer("/reset",session)
        self.assertFalse(session.turns)

    def test_unsupported_citation_cannot_finish(self):
        def invent(messages, structured):
            return '{"action":"answer","refs":["S99"],"text":"Ошибка"}'
        result = assistant_engine.analyze("Есть данные?", [], invent)
        self.assertFalse(result.sources)
        self.assertNotIn("S99", result.text)

    def test_live_changes_and_external_isolation(self):
        target = Path(store.DEMO).parent/"documents"
        target.mkdir()
        src = target/"rules.md"
        with patch.dict(os.environ, SELLER_DB=str(store.DEMO)):
            self.assertFalse(knowledge.search("закупка"))
            with patch.dict(os.environ, SELLER_DOCS_DIR=str(target)):
                src.write_text("# Правило\nСиний кабель хранится на первом складе.",encoding="utf8")
                self.assertIn("первом",bot.answer("/find кабель"))
                src.write_text("# Обновление\nСиний кабель хранится на втором складе.",encoding="utf8")
                self.assertIn("втором",bot.answer("/find кабель"))
                src.unlink()
                self.assertNotIn("складе", bot.answer("/find кабель"))

    def test_telegram_private_allowlist(self):
        def msg(i,chat_type):
            return {"update_id":i,"message":{"chat":{"id":i,"type":chat_type},"from":{"id":i},"text":"/stock"}}
        sent = []
        def api(url,payload):
            if url.endswith("getUpdates"):
                if payload["offset"]:
                    raise KeyboardInterrupt
                return {"result":[msg(1,"group"),msg(2,"private"),msg(3,"private")]}
            sent.append(payload)
            return {"ok":True}
        with patch.dict(os.environ, TELEGRAM_BOT_TOKEN="fixture",TELEGRAM_USER_IDS="1,2"), patch.object(bot,"post",api):
            with self.assertRaises(KeyboardInterrupt):
                bot.telegram()
        self.assertEqual([p["chat_id"] for p in sent],[2])
        self.assertIn("daily",sent[0]["text"])

    def test_hybrid_retrieval_cache_and_provider_outage(self):
        folder = Path(store.DEMO).parent
        docs = folder / "instructions"
        docs.mkdir()
        (docs/"warehouse.md").write_text("# Поставка\nОсмотреть коробки.", encoding="utf8")
        (docs/"bank.md").write_text("# Счёт\nПроверить перевод.", encoding="utf8")
        requests = []
        def vectors(config, texts):
            requests.append(texts)
            return [[0.0, 1.0] if "перевод" in t else [1.0, 0.0] for t in texts]
        with patch.dict(os.environ, SELLER_DOCS_DIR=str(docs), SELLER_INDEX_PATH=str(folder/"index.json"),
                        EMBEDDING_MODEL="fixture", EMBEDDING_BASE_URL="https://example.invalid/v1"):
            self.assertIsNone(knowledge.embedding_config()[2])
            with patch.object(knowledge, "embed", vectors):
                for _ in range(2):
                    self.assertIn("warehouse.md",knowledge.search("тара")[0]["source"])
                self.assertEqual([len(batch) for batch in requests],[2, 1, 1])
            with patch.object(knowledge, "embed", side_effect=TimeoutError):
                self.assertIn("bank.md", knowledge.search("перевод")[0]["source"])


if __name__ == "__main__":
    unittest.main()
