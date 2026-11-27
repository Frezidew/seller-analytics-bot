"""Document retrieval for the seller assistant."""

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


HERE = Path(__file__).resolve().parent
WORD = re.compile(r"[a-zа-я0-9]+")
STOP = set(
    "а без бы был быть в во вот все вы где да для до его если есть же за и из или им их к как "
    "какие какие-то какой когда ли мне можно мой мы на надо наш не него нет ни но ну о об один "
    "она они от по под при про с со свой себя так там те то того у уже чем что чтобы это я "
    "расскажи скажи пожалуйста нужно делать сделать узнать найти почему сколько".split()
)
ENDINGS = tuple(
    "иями ями ами ого ему ому ыми ими овать евать ировать ение ание ние ить ать ять еть "
    "ая яя ую юю ое ее ые ие ый ий ой ов ев ей ах ях ам ям ом ем ы и а я у ю е о ь й".split()
)


def tokens(value):
    for word in WORD.findall(value.lower().replace("ё", "е")):
        if word not in STOP and (len(word) > 1 or word.isdigit()):
            if re.fullmatch("[а-я]+", word):
                for ending in ENDINGS:
                    if word.endswith(ending) and len(word) - len(ending) >= 3:
                        word = word[:-len(ending)]
                        break
            yield word


def document_root():
    configured = os.getenv("SELLER_DOCS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return None if os.getenv("SELLER_DB") or os.getenv("SELLER_SHEET_ID") else HERE / "demo_docs"


def chunks(lines, filename):
    section = ""
    parts = []

    def emit():
        last = next(index for index in range(len(parts) - 1, -1, -1) if parts[index][1].strip())
        start, end = parts[0][0], parts[last][0]
        location = f"{filename} · {section}" if section else filename
        return {
            "source": f"{location} · строки {start}–{end}",
            "text": "\n".join(value for _, value in parts).strip(),
            "section": section,
        }

    size = 0
    for number, line in enumerate(lines, 1):
        heading = re.fullmatch(r"#{1,6}\s+(.+?)\s*#*\s*", line) if filename.lower().endswith(".md") else None
        if heading:
            if parts and size:
                yield emit()
            parts, size = [], 0
            section = heading[1]
            continue
        if not line.strip():
            if parts:
                parts.append((number, ""))
            continue
        for offset in range(0, len(line), 900):
            excerpt = line[offset:offset + 900]
            if not excerpt.strip():
                continue
            if size + len(excerpt) > 900 and parts:
                yield emit()
                parts, size = [], 0
            parts.append((number, excerpt))
            size += len(excerpt)
    if parts and size:
        yield emit()


@lru_cache(maxsize=128)
def read_chunks(path, filename, modified, changed, size):
    return tuple(chunks(path.read_text(encoding="utf-8-sig").splitlines(), filename))


def load_documents():
    root = document_root()
    documents = []
    if not root or not root.is_dir():
        return documents
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in {".md", ".txt"}:
            continue
        try:
            if not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            stat = path.stat()
            documents.extend(read_chunks(
                path, path.relative_to(root).as_posix(), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
            ))
        except (OSError, UnicodeError):
            continue
    return documents


def embedding_config():
    model = os.getenv("EMBEDDING_MODEL")
    chat_base = os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1"
    base = os.getenv("EMBEDDING_BASE_URL") or chat_base
    key = os.getenv("EMBEDDING_API_KEY")
    if not key and urlsplit(base)[:2] == urlsplit(chat_base)[:2]:
        key = os.getenv("LLM_API_KEY")
    return (base.rstrip("/") + "/embeddings", model, key) if model else None


def vector(values):
    magnitude = math.sqrt(sum(value * value for value in values))
    if not magnitude or not math.isfinite(magnitude):
        raise ValueError("Некорректный эмбеддинг")
    return [value / magnitude for value in values]


def embed(config, inputs):
    url, model, key = config
    payload = json.dumps({"model": model, "input": inputs}, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    request = Request(url, payload, headers)
    with urlopen(request, timeout=10) as response:
        rows = sorted(json.load(response)["data"], key=lambda row: row["index"])
    if [row["index"] for row in rows] != list(range(len(inputs))):
        raise ValueError("Неполный ответ сервиса эмбеддингов")
    return [vector(row["embedding"]) for row in rows]


def document_vectors(config, documents):
    url, model, _ = config
    signature = [str(document_root()), url, model]
    cache_path = Path(os.getenv("SELLER_INDEX_PATH") or HERE / ".knowledge-index.json").expanduser()
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        existing = cache["vectors"] if cache.get("signature") == signature else {}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        existing = {}
    passages = [f"{row['section']}\n{row['text']}" for row in documents]
    keys = [hashlib.sha256(passage.encode()).hexdigest() for passage in passages]
    indexed = {key: existing[key] for key in keys if key in existing}
    missing = {key: passage for key, passage in zip(keys, passages) if key not in indexed}
    missing_keys = list(missing)
    for start in range(0, len(missing_keys), 32):
        batch = missing_keys[start:start + 32]
        indexed.update(zip(batch, embed(config, [missing[key] for key in batch])))
    if indexed != existing:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=cache_path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump({"signature": signature, "vectors": indexed}, stream)
            temporary.replace(cache_path)
        except OSError:
            if temporary:
                temporary.unlink(missing_ok=True)
    return [indexed[key] for key in keys]


def lexical_scores(query, documents):
    keywords = sorted(set(tokens(query)))
    counts = [Counter(tokens(f"{doc['section']} {doc['text']}")) for doc in documents]
    if not keywords:
        return [0.0] * len(documents)
    frequencies = {word: sum(word in count for count in counts) for word in keywords}
    average = max(1, sum(sum(count.values()) for count in counts) / len(documents))
    scores = []
    for count in counts:
        length_factor = 0.25 + 0.75 * sum(count.values()) / average
        score = sum(
            math.log1p((len(documents) - frequencies[word] + 0.5) / (frequencies[word] + 0.5))
            * (count[word] * 2.2) / (count[word] + 1.2 * length_factor)
            for word in keywords if count[word]
        )
        scores.append(score)
    return scores


def search(query, limit=4):
    documents = load_documents()
    if not documents or not query.strip() or limit < 1:
        return []
    scores = lexical_scores(query, documents)
    config = embedding_config()
    if config:
        try:
            vectors = document_vectors(config, documents)
            target, = embed(config, [query])
            if any(len(item) != len(target) for item in vectors):
                raise ValueError("Размеры эмбеддингов различаются")
            strongest = max(scores) or 1
            similarities = [sum(left * right for left, right in zip(target, item)) for item in vectors]
            scores = [
                0.65 * lexical / strongest + 0.35 * max(0, similarity)
                if lexical > 0 or similarity >= 0.45 else 0
                for lexical, similarity in zip(scores, similarities)
            ]
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            pass
    ranked = sorted(zip(scores, documents), key=lambda item: item[0], reverse=True)
    return [
        {"source": item["source"], "text": item["text"]}
        for score, item in ranked[:limit] if score > 0
    ]


def main():
    parser = argparse.ArgumentParser(description="Поиск по базе знаний магазина")
    parser.add_argument("mode", choices=["status", "index", "search"])
    parser.add_argument("query", nargs="?")
    args = parser.parse_args()
    if args.mode == "search":
        if not args.query:
            parser.error("Для поиска укажите вопрос")
        print(json.dumps(search(args.query), ensure_ascii=False, indent=2))
        return
    documents = load_documents()
    config = embedding_config()
    mode = "лексический"
    if config:
        mode = "гибридный настроен"
        if args.mode == "index" and documents:
            try:
                document_vectors(config, documents)
                mode = "гибридный индекс готов"
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                mode = "лексический (сервис эмбеддингов недоступен)"
    print(f"Документы: {document_root() or 'не подключены'}")
    print(f"Фрагменты: {len(documents)}. Режим: {mode}.")


if __name__ == "__main__":
    main()
