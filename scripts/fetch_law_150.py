#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LAW_DIR = DATA_DIR / "law_150"
ARTICLES_DIR = LAW_DIR / "articles"
INDEX_PATH = LAW_DIR / "index.json"

LAW_URL = "https://www.consultant.ru/document/cons_doc_LAW_12679/"
LAW_PATH_PREFIX = "/document/cons_doc_LAW_12679/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0 Safari/537.36 "
    "OruzheyniyZakonBot/2.1"
)

ATTEMPTS = 3
TIMEOUT_SECONDS = 40
MIN_ARTICLE_LENGTH = 80
MAX_ARTICLE_LENGTH = 500_000


class LinkTextParser(HTMLParser):
    """Collects visible text and anchor text/href pairs."""

    SKIP_TAGS = {
        "script",
        "style",
        "svg",
        "noscript",
        "template",
        "canvas",
    }

    BREAK_TAGS = {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "br",
        "hr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered = tag.lower()

        if lowered in self.SKIP_TAGS:
            self._skip_depth += 1
            return

        if self._skip_depth:
            return

        if lowered == "title":
            self._in_title = True

        if lowered == "a":
            attributes = dict(attrs)
            self._anchor_href = attributes.get("href")
            self._anchor_parts = []

        if lowered in self.BREAK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()

        if lowered in self.SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return

        if self._skip_depth:
            return

        if lowered == "title":
            self._in_title = False
            self.title = normalize_text(" ".join(self._title_parts))

        if lowered == "a":
            if self._anchor_href:
                anchor_text = normalize_inline(
                    " ".join(self._anchor_parts)
                )
                self.links.append(
                    (self._anchor_href, anchor_text)
                )

            self._anchor_href = None
            self._anchor_parts = []

        if lowered in self.BREAK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return

        if self._in_title:
            self._title_parts.append(data)

        if self._anchor_href is not None:
            self._anchor_parts.append(data)

        self._parts.append(data)

    def text(self) -> str:
        return normalize_text(" ".join(self._parts))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_inline(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = value.replace("\u200b", "")
    value = value.replace("\ufeff", "")
    return re.sub(r"\s+", " ", value).strip()


def normalize_text(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = value.replace("\u200b", "")
    value = value.replace("\ufeff", "")

    lines: list[str] = []

    for raw_line in value.splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", raw_line).strip()

        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue

        lines.append(line)

    while lines and not lines[-1]:
        lines.pop()

    return "\n".join(lines)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def decode_response(raw: bytes, content_type: str) -> str:
    charset_match = re.search(
        r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)",
        content_type,
        flags=re.IGNORECASE,
    )

    candidates: list[str] = []

    if charset_match:
        candidates.append(charset_match.group(1))

    prefix = raw[:4096].decode("ascii", errors="ignore")
    meta_match = re.search(
        r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)",
        prefix,
        flags=re.IGNORECASE,
    )

    if meta_match:
        candidates.append(meta_match.group(1))

    candidates.extend(("utf-8-sig", "utf-8", "windows-1251"))

    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

    return raw.decode("utf-8", errors="replace")


def fetch_html(url: str) -> str:
    last_error: Exception | None = None

    for attempt in range(1, ATTEMPTS + 1):
        request = Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
                "Cache-Control": "no-cache",
                "User-Agent": USER_AGENT,
            },
        )

        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read()
                status = int(getattr(response, "status", 200))
                content_type = response.headers.get("Content-Type", "")

            if status != 200:
                raise RuntimeError(f"HTTP {status}")

            if not raw:
                raise RuntimeError("Получен пустой ответ")

            return decode_response(raw, content_type)

        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            RuntimeError,
        ) as error:
            last_error = error

            if attempt < ATTEMPTS:
                time.sleep(2 ** attempt)

    detail = str(last_error).strip() if last_error else "неизвестная ошибка"
    raise RuntimeError(
        f"Источник недоступен после {ATTEMPTS} попыток: {detail}"
    )


def parse_html(html: str) -> LinkTextParser:
    parser = LinkTextParser()
    parser.feed(html)
    parser.close()
    return parser


def article_sort_key(number: str) -> tuple[int, ...]:
    return tuple(int(part) for part in number.split("."))


def discover_articles(
    parser: LinkTextParser,
) -> list[dict[str, str]]:
    article_pattern = re.compile(
        r"^Статья\s+(\d+(?:\.\d+)?)\.\s*(.+)$",
        flags=re.IGNORECASE,
    )

    discovered: dict[str, dict[str, str]] = {}

    for href, anchor_text in parser.links:
        if not href:
            continue

        match = article_pattern.match(anchor_text)

        if not match:
            continue

        absolute_url = urljoin(LAW_URL, href)

        if LAW_PATH_PREFIX not in absolute_url:
            continue

        # Main document URL is not an article URL.
        tail = absolute_url.split(LAW_PATH_PREFIX, maxsplit=1)[1]
        tail = tail.strip("/")

        if not re.fullmatch(r"[0-9a-fA-F]{16,64}", tail):
            continue

        number = match.group(1)
        title = normalize_inline(match.group(2))

        discovered.setdefault(
            number,
            {
                "number": number,
                "title": title,
                "heading": f"Статья {number}. {title}",
                "url": absolute_url,
            },
        )

    return sorted(
        discovered.values(),
        key=lambda item: article_sort_key(item["number"]),
    )


def extract_revision_date(text: str) -> str | None:
    matches = re.findall(
        r"\(ред\.\s*от\s*(\d{2}\.\d{2}\.\d{4})\)",
        text,
        flags=re.IGNORECASE,
    )

    return matches[-1] if matches else None


def find_article_start(
    text: str,
    heading: str,
    number: str,
) -> int:
    exact = text.find(heading)

    if exact >= 0:
        return exact

    pattern = re.compile(
        rf"Статья\s+{re.escape(number)}\.\s+[^\n]+",
        flags=re.IGNORECASE,
    )
    match = pattern.search(text)

    if match:
        return match.start()

    raise RuntimeError(
        f"Не найден заголовок статьи {number}"
    )


def find_article_end(
    candidate: str,
    number: str,
) -> int:
    search_from = min(len(candidate), 250)

    footer_patterns = (
        re.compile(
            rf"(?:^|\n)Ст\.\s*{re.escape(number)}\.",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|\n)Гражданский кодекс \(ГК РФ\)",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|\n)Контактная информация",
            flags=re.IGNORECASE,
        ),
    )

    ends: list[int] = []

    for pattern in footer_patterns:
        match = pattern.search(candidate, pos=search_from)

        if match:
            ends.append(match.start())

    return min(ends) if ends else len(candidate)


def remove_consultant_service_blocks(
    article_text: str,
) -> str:
    lines = article_text.splitlines()
    result: list[str] = []

    skip_preface = True

    service_patterns = (
        re.compile(
            r"^Перспективы и риски.*$",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"^Ситуации, связанные со ст\..*$",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"^Путеводитель по.*$",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"^Готовое решение.*$",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"^Форма:.*$",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"^Открыть полный текст документа$",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"^КонсультантПлюс: примечание\.?$",
            flags=re.IGNORECASE,
        ),
    )

    for index, line in enumerate(lines):
        stripped = line.strip()

        # Always retain the article heading.
        if index == 0:
            result.append(stripped)
            continue

        if not stripped:
            if result and result[-1] != "":
                result.append("")
            continue

        if any(pattern.match(stripped) for pattern in service_patterns):
            continue

        # Before the normative text begins, Consultant may show a list of
        # practical dispute situations. Skip only bullet-like lines there.
        if skip_preface and (
            stripped.startswith("- ")
            or stripped.startswith("• ")
        ):
            continue

        # The first normal paragraph or amendment note marks the start
        # of the article content.
        skip_preface = False
        result.append(stripped)

    return normalize_text("\n".join(result))


def clean_normative_text(source_text: str) -> str:
    """
    Removes only clearly editorial parenthetical notes.

    Non-parenthetical statements such as “часть утратила силу” remain,
    because they are legally material to the current consolidated text.
    """
    result: list[str] = []

    editorial_patterns = (
        re.compile(
            r"^\(в ред\..*\)$",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"^\(см\. текст в предыдущей редакции\)$",
            flags=re.IGNORECASE,
        ),
        re.compile(
            r"^\((?:часть|пункт|абзац|статья).+"
            r"(?:введен|введена|введены|в ред\.).*\)$",
            flags=re.IGNORECASE,
        ),
    )

    for line in source_text.splitlines():
        stripped = line.strip()

        if any(pattern.match(stripped) for pattern in editorial_patterns):
            continue

        result.append(line)

    return normalize_text("\n".join(result))


def extract_article(
    article: dict[str, str],
) -> dict[str, Any]:
    html = fetch_html(article["url"])
    parser = parse_html(html)
    page_text = parser.text()

    start = find_article_start(
        page_text,
        article["heading"],
        article["number"],
    )

    candidate = page_text[start:]
    end = find_article_end(
        candidate,
        article["number"],
    )

    source_text = candidate[:end].strip()
    source_text = remove_consultant_service_blocks(source_text)

    clean_text = clean_normative_text(source_text)

    if len(clean_text) < MIN_ARTICLE_LENGTH:
        raise RuntimeError(
            f"Статья {article['number']} подозрительно короткая: "
            f"{len(clean_text)} символов"
        )

    if len(clean_text) > MAX_ARTICLE_LENGTH:
        clean_text = clean_text[:MAX_ARTICLE_LENGTH]

    return {
        "schema_version": 1,
        "law_number": "150-ФЗ",
        "law_date": "13.12.1996",
        "article_number": article["number"],
        "title": article["title"],
        "heading": article["heading"],
        "revision_date": extract_revision_date(page_text),
        "source_type": "public_legal_system",
        "source_name": "КонсультантПлюс",
        "source_url": article["url"],
        "fetched_at": utc_now(),
        "source_sha256": sha256_text(source_text),
        "clean_sha256": sha256_text(clean_text),
        "source_text": source_text,
        "clean_text": clean_text,
    }


def extract_preamble(main_text: str) -> str:
    start_phrase = "Настоящий Федеральный закон регулирует правоотношения"
    start = main_text.find(start_phrase)

    if start < 0:
        return ""

    article_one = main_text.find("\nСтатья 1.", start)

    if article_one < 0:
        article_one = main_text.find("Статья 1.", start)

    candidate = (
        main_text[start:article_one]
        if article_one > start
        else main_text[start:start + 5000]
    )

    candidate = re.sub(
        r"\n?\(преамбула в ред\..*?\)"
        r"(?:\n\(см\. текст в предыдущей редакции\))?",
        "",
        candidate,
        flags=re.IGNORECASE | re.DOTALL,
    )

    return normalize_text(candidate)


def main() -> int:
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)

    main_html = fetch_html(LAW_URL)
    main_parser = parse_html(main_html)
    main_text = main_parser.text()

    articles = discover_articles(main_parser)

    if len(articles) < 35:
        raise RuntimeError(
            "Обнаружено слишком мало статей закона: "
            f"{len(articles)}"
        )

    revision_date = extract_revision_date(main_text)
    preamble = extract_preamble(main_text)

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for position, article in enumerate(articles, start=1):
        number = article["number"]

        try:
            document = extract_article(article)
            file_name = f"article_{number.replace('.', '_')}.json"
            write_json(ARTICLES_DIR / file_name, document)

            results.append(
                {
                    "number": number,
                    "title": article["title"],
                    "heading": article["heading"],
                    "sort_order": position,
                    "revision_date": document["revision_date"],
                    "source_url": article["url"],
                    "file": f"data/law_150/articles/{file_name}",
                    "clean_sha256": document["clean_sha256"],
                    "content_length": len(document["clean_text"]),
                }
            )

            print(
                f"[OK] Статья {number}: "
                f"{len(document['clean_text'])} символов"
            )

        except Exception as error:
            errors.append(
                {
                    "number": number,
                    "title": article["title"],
                    "url": article["url"],
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            print(
                f"[ERROR] Статья {number}: "
                f"{type(error).__name__}: {error}"
            )

    combined_hash = sha256_text(
        "\n".join(
            f"{item['number']}:{item['clean_sha256']}"
            for item in results
        )
    )

    index = {
        "schema_version": 1,
        "law_slug": "federal-law-150-fz",
        "title": (
            "Федеральный закон от 13.12.1996 № 150-ФЗ "
            "«Об оружии»"
        ),
        "short_title": "Закон «Об оружии»",
        "law_number": "150-ФЗ",
        "law_date": "13.12.1996",
        "revision_date": revision_date,
        "generated_at": utc_now(),
        "source_type": "public_legal_system",
        "source_name": "КонсультантПлюс",
        "source_url": LAW_URL,
        "admin_review_required": True,
        "automatic_publication": False,
        "preamble": preamble,
        "article_count_discovered": len(articles),
        "article_count_saved": len(results),
        "error_count": len(errors),
        "combined_sha256": combined_hash,
        "articles": results,
        "errors": errors,
    }

    write_json(INDEX_PATH, index)

    print(
        f"Обнаружено статей: {len(articles)}; "
        f"сохранено: {len(results)}; ошибок: {len(errors)}"
    )

    return 0 if results else 2


if __name__ == "__main__":
    sys.exit(main())
