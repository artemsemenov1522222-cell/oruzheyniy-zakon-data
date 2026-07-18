#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
STATUS_PATH = DATA_DIR / "status.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0 Safari/537.36 "
    "OruzheyniyZakonBot/2.0"
)

ATTEMPTS = 2
TIMEOUT_SECONDS = 30
MAX_TEXT_LENGTH = 600_000


@dataclass(frozen=True)
class Source:
    source_id: str
    name: str
    url: str
    source_type: str
    trust_level: str
    intended_use: str
    content_mode: str = "snapshot"


SOURCES = (
    Source(
        source_id="pravo_236_api",
        name="Официальный портал правовой информации — 236-ФЗ",
        url=(
            "https://publication.pravo.gov.ru/api/Document"
            "?eoNumber=0001202607040025"
        ),
        source_type="official_api",
        trust_level="primary",
        intended_use="Официальные метаданные и контрольное подтверждение",
        content_mode="json",
    ),
    Source(
        source_id="rg_236_fz",
        name="Российская газета — Федеральный закон № 236-ФЗ",
        url="https://rg.ru/documents/2026/07/08/fz-236-doc.html",
        source_type="official_publication_copy",
        trust_level="primary_publication",
        intended_use=(
            "Резервное получение опубликованного текста закона "
            "и дат вступления в силу"
        ),
        content_mode="rg_document",
    ),
    Source(
        source_id="consultant_150_fz",
        name="КонсультантПлюс — Федеральный закон № 150-ФЗ",
        url="https://www.consultant.ru/document/cons_doc_LAW_12679/",
        source_type="public_legal_system",
        trust_level="verification",
        intended_use=(
            "Контроль номера действующей редакции и обнаружение изменений; "
            "не источник авторских комментариев"
        ),
        content_mode="metadata",
    ),
    Source(
        source_id="rosguard_weapon_literacy",
        name="Росгвардия — Оружейная грамотность",
        url="https://rosguard.gov.ru/ru/page/index/oruzhejnaya-gramotnost",
        source_type="official_agency",
        trust_level="primary_guidance",
        intended_use=(
            "Перечень профильных НПА и официальные разъяснения Росгвардии"
        ),
        content_mode="metadata",
    ),
)


class VisibleTextParser(HTMLParser):
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

        if lowered in self.BREAK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return

        if self._in_title:
            self._title_parts.append(data)

        self._parts.append(data)

    def text(self) -> str:
        return normalize_text(" ".join(self._parts))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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

    candidates = []

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


def fetch(source: Source) -> tuple[bytes, str, int]:
    last_error: Exception | None = None

    for attempt in range(1, ATTEMPTS + 1):
        request = Request(
            source.url,
            headers={
                "Accept": (
                    "application/json"
                    if source.content_mode == "json"
                    else "text/html,application/xhtml+xml"
                ),
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

            return raw, content_type, status

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


def parse_html(raw: bytes, content_type: str) -> tuple[str, str]:
    html = decode_response(raw, content_type)
    parser = VisibleTextParser()
    parser.feed(html)
    parser.close()

    text = parser.text()

    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH]

    return parser.title, text


def extract_revision_metadata(text: str) -> dict[str, Any]:
    revisions = sorted(
        set(
            re.findall(
                r"(?:ред\.|редакц(?:ия|ии|ию))\s*"
                r"(?:от\s*)?(\d{2}\.\d{2}\.\d{4})",
                text,
                flags=re.IGNORECASE,
            )
        )
    )

    future_revision = bool(
        re.search(
            r"(подготовлена|предусмотрена)\s+редакция"
            r".{0,120}(не\s+вступивш|вступающ)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        or re.search(
            r"изменения.{0,120}не\s+вступили\s+в\s+силу",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )

    return {
        "revision_dates_found": revisions[-20:],
        "latest_revision_date": revisions[-1] if revisions else None,
        "future_revision_mentioned": future_revision,
    }


def extract_rg_document(text: str) -> str:
    title_pattern = re.compile(
        r"Федеральный закон от 4 июля 2026 г\.\s*"
        r"N\s*236-ФЗ\s*"
        r"\"О внесении изменений в Федеральный закон "
        r"\"Об оружии\"\"",
        flags=re.IGNORECASE,
    )

    match = title_pattern.search(text)

    if not match:
        match = re.search(
            r"Федеральный закон.{0,200}236-ФЗ.{0,200}Об оружии",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

    start = match.start() if match else 0
    candidate = text[start:]

    footer_markers = (
        "\nПоделиться\n",
        "\nНа сайте rg.ru применяются",
        "\nТематика:",
        "\nГлавное сегодня",
    )

    for marker in footer_markers:
        index = candidate.find(marker)
        if index > 500:
            candidate = candidate[:index]

    if len(candidate) < 800:
        raise RuntimeError(
            "Не удалось выделить полный текст документа на странице"
        )

    return candidate[:MAX_TEXT_LENGTH]


def process_json_source(
    source: Source,
    raw: bytes,
    content_type: str,
    http_status: int,
) -> dict[str, Any]:
    text = decode_response(raw, content_type)
    payload = json.loads(text)

    if not isinstance(payload, dict):
        raise RuntimeError("API вернул JSON не в виде объекта")

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return {
        "schema_version": 2,
        "source_id": source.source_id,
        "source_name": source.name,
        "source_type": source.source_type,
        "trust_level": source.trust_level,
        "intended_use": source.intended_use,
        "url": source.url,
        "http_status": http_status,
        "fetched_at": utc_now(),
        "sha256": sha256_text(canonical),
        "payload": payload,
    }


def process_html_source(
    source: Source,
    raw: bytes,
    content_type: str,
    http_status: int,
) -> dict[str, Any]:
    title, text = parse_html(raw, content_type)

    if len(text) < 300:
        raise RuntimeError(
            f"Слишком мало извлечённого текста: {len(text)} символов"
        )

    snapshot: dict[str, Any] = {
        "schema_version": 2,
        "source_id": source.source_id,
        "source_name": source.name,
        "source_type": source.source_type,
        "trust_level": source.trust_level,
        "intended_use": source.intended_use,
        "url": source.url,
        "http_status": http_status,
        "fetched_at": utc_now(),
        "page_title": title,
    }

    if source.content_mode == "rg_document":
        document_text = extract_rg_document(text)
        snapshot["sha256"] = sha256_text(document_text)
        snapshot["content_length"] = len(document_text)
        snapshot["content"] = document_text
        snapshot["metadata"] = {
            "document_number": "236-ФЗ",
            "document_date": "04.07.2026",
            "publication_date": "08.07.2026",
            "changes_document": "Федеральный закон № 150-ФЗ «Об оружии»",
        }
        return snapshot

    metadata = extract_revision_metadata(text)

    snapshot["sha256"] = sha256_text(text)
    snapshot["content_length"] = len(text)
    snapshot["metadata"] = metadata
    snapshot["excerpt"] = text[:2500]

    return snapshot


def process_source(source: Source) -> dict[str, Any]:
    raw, content_type, http_status = fetch(source)

    if source.content_mode == "json":
        return process_json_source(
            source,
            raw,
            content_type,
            http_status,
        )

    return process_html_source(
        source,
        raw,
        content_type,
        http_status,
    )


def main() -> int:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    checked_at = utc_now()
    results: list[dict[str, Any]] = []
    success_count = 0

    for source in SOURCES:
        try:
            snapshot = process_source(source)
            snapshot_path = (
                SNAPSHOTS_DIR / f"{source.source_id}.json"
            )
            write_json(snapshot_path, snapshot)

            results.append(
                {
                    "source_id": source.source_id,
                    "name": source.name,
                    "url": source.url,
                    "source_type": source.source_type,
                    "trust_level": source.trust_level,
                    "status": "ok",
                    "checked_at": checked_at,
                    "sha256": snapshot["sha256"],
                    "snapshot_file": (
                        f"data/snapshots/{source.source_id}.json"
                    ),
                }
            )
            success_count += 1
            print(f"[OK] {source.source_id}: {source.name}")

        except Exception as error:
            previous_snapshot = (
                SNAPSHOTS_DIR / f"{source.source_id}.json"
            )

            results.append(
                {
                    "source_id": source.source_id,
                    "name": source.name,
                    "url": source.url,
                    "source_type": source.source_type,
                    "trust_level": source.trust_level,
                    "status": "error",
                    "checked_at": checked_at,
                    "previous_snapshot_available": (
                        previous_snapshot.exists()
                    ),
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            print(
                f"[ERROR] {source.source_id}: "
                f"{type(error).__name__}: {error}"
            )

    total = len(SOURCES)
    failed_count = total - success_count

    if success_count == total:
        overall_status = "ok"
    elif success_count:
        overall_status = "partial"
    else:
        overall_status = "error"

    status = {
        "schema_version": 2,
        "generated_at": utc_now(),
        "overall_status": overall_status,
        "total": total,
        "successful": success_count,
        "failed": failed_count,
        "policy": {
            "automatic_publication": False,
            "admin_review_required": True,
            "official_portal_is_preferred": True,
            "fallback_sources_are_labeled": True,
        },
        "sources": results,
    }

    write_json(STATUS_PATH, status)

    print(
        f"Проверено источников: {total}; "
        f"успешно: {success_count}; ошибок: {failed_count}; "
        f"статус: {overall_status}"
    )

    return 0 if success_count else 2


if __name__ == "__main__":
    sys.exit(main())
