#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DOCUMENTS_DIR = DATA_DIR / "documents"
WATCHLIST_PATH = DATA_DIR / "watchlist.json"
STATUS_PATH = DATA_DIR / "status.json"

API_URL = "https://publication.pravo.gov.ru/api/Document?eoNumber={eo_number}"
PAGE_URL = "https://publication.pravo.gov.ru/document/{eo_number}"

USER_AGENT = (
    "Mozilla/5.0 (compatible; OruzheyniyZakonBot/1.0; "
    "+https://github.com/artemsemenov1522222-cell/oruzheyniy-zakon-data)"
)

ATTEMPTS = 3
TIMEOUT_SECONDS = 35


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def request_json(url: str) -> dict[str, Any]:
    last_error: Exception | None = None

    for attempt in range(1, ATTEMPTS + 1):
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read()
                status = getattr(response, "status", 200)

            if status != 200:
                raise RuntimeError(f"HTTP {status}")

            payload = json.loads(raw.decode("utf-8-sig"))

            if not isinstance(payload, dict):
                raise RuntimeError("API вернул не объект JSON")

            return payload

        except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as error:
            last_error = error
            if attempt < ATTEMPTS:
                time.sleep(2 ** attempt)

    raise RuntimeError(
        f"Источник недоступен после {ATTEMPTS} попыток: {last_error}"
    )


def normalized_document(
    eo_number: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return {
        "schema_version": 1,
        "eo_number": eo_number,
        "fetched_at": utc_now(),
        "api_url": API_URL.format(eo_number=eo_number),
        "official_page_url": PAGE_URL.format(eo_number=eo_number),
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "payload": payload,
    }


def main() -> None:
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)

    watchlist = load_json(WATCHLIST_PATH)
    documents = watchlist.get("documents", [])

    if not isinstance(documents, list):
        raise SystemExit("data/watchlist.json: поле documents должно быть списком")

    results: list[dict[str, Any]] = []
    success_count = 0

    for item in documents:
        eo_number = str(item.get("eo_number", "")).strip()
        label = str(item.get("label", "")).strip()

        if not eo_number.isdigit():
            results.append(
                {
                    "eo_number": eo_number,
                    "label": label,
                    "status": "invalid",
                    "checked_at": utc_now(),
                    "error": "Некорректный номер электронного опубликования",
                }
            )
            continue

        try:
            payload = request_json(API_URL.format(eo_number=eo_number))
            document = normalized_document(eo_number, payload)
            write_json(DOCUMENTS_DIR / f"{eo_number}.json", document)

            results.append(
                {
                    "eo_number": eo_number,
                    "label": label,
                    "status": "ok",
                    "checked_at": utc_now(),
                    "sha256": document["sha256"],
                    "file": f"data/documents/{eo_number}.json",
                    "official_page_url": document["official_page_url"],
                }
            )
            success_count += 1

        except Exception as error:
            results.append(
                {
                    "eo_number": eo_number,
                    "label": label,
                    "status": "error",
                    "checked_at": utc_now(),
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    total = len(results)

    status = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "source": "Официальный интернет-портал правовой информации",
        "source_help_url": "https://publication.pravo.gov.ru/help",
        "total": total,
        "successful": success_count,
        "failed": total - success_count,
        "overall_status": (
            "ok"
            if total > 0 and success_count == total
            else "partial"
            if success_count > 0
            else "error"
        ),
        "documents": results,
    }

    write_json(STATUS_PATH, status)

    print(
        f"Проверено: {total}; успешно: {success_count}; "
        f"ошибок: {total - success_count}"
    )


if __name__ == "__main__":
    main()
