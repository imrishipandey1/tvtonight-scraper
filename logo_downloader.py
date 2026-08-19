#!/usr/bin/env python3
"""Cache TitanTV show logos by exact show title and replace schedule URLs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import unicodedata
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

import config

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except ImportError:
    Image = None
    ImageOps = None
    UnidentifiedImageError = Exception


ROOT_DIR = Path(__file__).resolve().parent
REQUEST_TIMEOUT_SECONDS = 30
DOWNLOAD_RETRIES = 5
CACHE_QUERY_BATCH_SIZE = 500


@dataclass(frozen=True)
class ErrorDetail:
    callsign: str
    url: str
    reason: str


@dataclass
class ScannedShow:
    show_key: str
    show_name: str
    source_url: str
    callsigns: set[str]


@dataclass(frozen=True)
class ShowTask:
    show_key: str
    show_name: str
    source_url: str
    canonical_callsign: str
    canonical_path: Path
    targets: tuple[tuple[str, Path], ...]


@dataclass(frozen=True)
class DownloadTask:
    source_url: str
    canonical_callsign: str
    canonical_path: Path
    shows: tuple[ShowTask, ...]


@dataclass(frozen=True)
class DownloadResult:
    task: DownloadTask
    success: bool
    reason: str = ""


@dataclass
class ScanStats:
    json_files: int = 0
    logo_references: int = 0
    already_replaced: int = 0


class LogoError(Exception):
    """A recoverable logo-processing error."""


def project_path(value: str) -> Path:
    configured_path = Path(value)
    return configured_path if configured_path.is_absolute() else ROOT_DIR / configured_path


SCHEDULE_DIR = project_path(config.SCHEDULE_DIR)
LOGO_DIR = project_path(config.LOGO_DIR)
CACHE_DB = project_path(config.CACHE_DB)
ERROR_FILE = project_path(config.ERROR_FILE)
CDN_URL = config.CDN_URL.rstrip("/") + "/"
FALLBACK_LOGO_URL = config.FALLBACK_LOGO_URL.strip()


def iter_schedule_files() -> Iterator[Path]:
    if not SCHEDULE_DIR.is_dir():
        raise LogoError(f"Schedule directory does not exist: {SCHEDULE_DIR}")
    for current_root, directory_names, filenames in os.walk(SCHEDULE_DIR):
        directory_names.sort()
        for filename in sorted(filenames):
            if filename.lower().endswith(".json"):
                yield Path(current_root) / filename


def load_json(json_path: Path) -> dict[str, Any]:
    try:
        with json_path.open("r", encoding="utf-8") as json_file:
            data = json.load(json_file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LogoError(f"Could not read JSON: {error}") from error
    if not isinstance(data, dict):
        raise LogoError("JSON root must be an object.")
    return data


def is_managed_logo_url(value: str) -> bool:
    return value.startswith(CDN_URL) or value == FALLBACK_LOGO_URL


def is_download_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalized_show_key(show_name: str) -> str:
    normalized = unicodedata.normalize("NFKC", show_name)
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def scan_schedules(
    errors: list[ErrorDetail],
) -> tuple[dict[str, ScannedShow], set[str], ScanStats]:
    shows: dict[str, ScannedShow] = {}
    source_urls: set[str] = set()
    stats = ScanStats()

    for json_path in iter_schedule_files():
        stats.json_files += 1
        callsign = json_path.parent.name
        try:
            payload = load_json(json_path)
        except LogoError as error:
            errors.append(ErrorDetail(callsign, str(json_path), str(error)))
            continue

        schedule = payload.get("schedule")
        if not isinstance(schedule, list):
            errors.append(ErrorDetail(callsign, str(json_path), "Missing list field: schedule"))
            continue

        for event in schedule:
            if not isinstance(event, dict):
                continue
            logo_url = event.get("l")
            show_name = event.get("n")
            if not isinstance(logo_url, str) or not logo_url.strip():
                continue
            if not isinstance(show_name, str) or not show_name.strip():
                errors.append(ErrorDetail(callsign, logo_url.strip(), "Missing show name"))
                continue

            logo_url = logo_url.strip()
            stats.logo_references += 1
            if is_managed_logo_url(logo_url):
                stats.already_replaced += 1
                continue
            if not is_download_url(logo_url):
                errors.append(ErrorDetail(callsign, logo_url, "Logo URL must use http or https"))
                continue

            show_key = normalized_show_key(show_name)
            source_urls.add(logo_url)
            existing_show = shows.get(show_key)
            if existing_show:
                existing_show.callsigns.add(callsign)
            else:
                shows[show_key] = ScannedShow(
                    show_key=show_key,
                    show_name=show_name.strip(),
                    source_url=logo_url,
                    callsigns={callsign},
                )

    return shows, source_urls, stats


def safe_component(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    safe_value = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip(".-")
    if not safe_value or safe_value in {".", ".."}:
        return "unknown"
    return safe_value[:120]


def show_filename(show_name: str) -> str:
    return safe_component(show_name).casefold()


def relative_to_root(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def cache_path(local_path: str) -> Path:
    stored_path = Path(local_path)
    return stored_path if stored_path.is_absolute() else ROOT_DIR / stored_path


def build_tasks(shows: dict[str, ScannedShow]) -> list[ShowTask]:
    filename_owners: dict[tuple[str, str], str] = {}
    tasks: list[ShowTask] = []

    for show_key in sorted(shows):
        show = shows[show_key]
        identifier = show_filename(show.show_name)
        targets: list[tuple[str, Path]] = []
        for callsign in sorted(show.callsigns):
            safe_callsign = safe_component(callsign)
            output_identifier = identifier
            owner_key = (safe_callsign, output_identifier)
            owner = filename_owners.get(owner_key)
            if owner and owner != show_key:
                suffix = hashlib.sha256(show_key.encode("utf-8")).hexdigest()[:10]
                output_identifier = f"{identifier}-{suffix}"
            filename_owners[(safe_callsign, output_identifier)] = show_key
            targets.append((callsign, LOGO_DIR / safe_callsign / f"{output_identifier}.webp"))

        canonical_callsign, canonical_path = targets[0]
        tasks.append(
            ShowTask(
                show_key=show.show_key,
                show_name=show.show_name,
                source_url=show.source_url,
                canonical_callsign=canonical_callsign,
                canonical_path=canonical_path,
                targets=tuple(targets),
            )
        )
    return tasks


def open_cache() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(CACHE_DB)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS logos (
            id INTEGER PRIMARY KEY,
            source_url TEXT UNIQUE,
            local_path TEXT,
            status TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS show_logos (
            show_key TEXT PRIMARY KEY,
            show_name TEXT,
            source_url TEXT,
            local_path TEXT,
            status TEXT
        )
        """
    )
    connection.commit()
    return connection


def chunks(values: list[str], chunk_size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), chunk_size):
        yield values[index : index + chunk_size]


def read_url_cache_rows(connection: sqlite3.Connection, urls: Iterable[str]) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for batch in chunks(list(urls), CACHE_QUERY_BATCH_SIZE):
        placeholders = ",".join("?" for _ in batch)
        query = f"SELECT source_url, local_path, status FROM logos WHERE source_url IN ({placeholders})"
        for source_url, local_path, status in connection.execute(query, batch):
            rows[str(source_url)] = (str(local_path or ""), str(status or ""))
    return rows


def read_show_cache_rows(
    connection: sqlite3.Connection, show_keys: Iterable[str]
) -> dict[str, tuple[str, str, str]]:
    rows: dict[str, tuple[str, str, str]] = {}
    for batch in chunks(list(show_keys), CACHE_QUERY_BATCH_SIZE):
        placeholders = ",".join("?" for _ in batch)
        query = (
            "SELECT show_key, source_url, local_path, status "
            f"FROM show_logos WHERE show_key IN ({placeholders})"
        )
        for show_key, source_url, local_path, status in connection.execute(query, batch):
            rows[str(show_key)] = (str(source_url or ""), str(local_path or ""), str(status or ""))
    return rows


def materialize_target(source: Path, destination: Path) -> None:
    if destination.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def fetch_image(source_url: str) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }
    request = Request(source_url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", response.getcode())
                if not 200 <= status < 300:
                    raise LogoError(f"HTTP {status}")
                return response.read()
        except HTTPError as error:
            if error.code < 500 and error.code not in {408, 429}:
                raise LogoError(f"HTTP {error.code}") from error
            last_error = LogoError(f"HTTP {error.code}")
        except (URLError, TimeoutError, socket.timeout) as error:
            last_error = LogoError(str(getattr(error, "reason", error)))
        if attempt + 1 < DOWNLOAD_RETRIES:
            time.sleep(2**attempt)
    raise LogoError(str(last_error or "Download failed"))


def convert_to_webp(image_data: bytes, destination: Path) -> None:
    if Image is None or ImageOps is None:
        raise LogoError("Pillow is not installed. Run: python3 -m pip install Pillow")

    temporary_path: Path | None = None
    try:
        with Image.open(BytesIO(image_data)) as original_image:
            original_image.load()
            working_image = ImageOps.exif_transpose(original_image)
            if len(image_data) >= config.SMALL_IMAGE_LIMIT and working_image.width > config.MAX_WIDTH:
                new_height = max(1, round(working_image.height * config.MAX_WIDTH / working_image.width))
                resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                working_image = working_image.resize((config.MAX_WIDTH, new_height), resampling)

            has_alpha = "A" in working_image.getbands() or "transparency" in working_image.info
            webp_image = working_image.convert("RGBA" if has_alpha else "RGB")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                webp_image.save(temporary_file, format="WEBP", quality=90, method=4)
            os.replace(temporary_path, destination)
            temporary_path = None
    except UnidentifiedImageError as error:
        raise LogoError("Downloaded content is not a valid image") from error
    except OSError as error:
        raise LogoError(f"Image conversion failed: {error}") from error
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def download_task(task: DownloadTask) -> DownloadResult:
    try:
        image_data = fetch_image(task.source_url)
        convert_to_webp(image_data, task.canonical_path)
        return DownloadResult(task, True)
    except Exception as error:
        return DownloadResult(task, False, str(error))


def upsert_url_cache_rows(connection: sqlite3.Connection, rows: Iterable[tuple[str, str, str]]) -> None:
    connection.executemany(
        """
        INSERT INTO logos (source_url, local_path, status)
        VALUES (?, ?, ?)
        ON CONFLICT(source_url) DO UPDATE SET
            local_path = excluded.local_path,
            status = excluded.status
        """,
        rows,
    )


def upsert_show_cache_rows(
    connection: sqlite3.Connection, rows: Iterable[tuple[str, str, str, str, str]]
) -> None:
    connection.executemany(
        """
        INSERT INTO show_logos (show_key, show_name, source_url, local_path, status)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(show_key) DO UPDATE SET
            show_name = excluded.show_name,
            source_url = excluded.source_url,
            local_path = excluded.local_path,
            status = excluded.status
        """,
        rows,
    )


def cdn_url(callsign: str, local_path: Path) -> str:
    return f"{CDN_URL}{quote(safe_component(callsign), safe='')}/{quote(local_path.name, safe='')}"


def make_show_available(
    task: ShowTask,
    source_path: Path,
    replacements: dict[tuple[str, str], str],
    errors: list[ErrorDetail],
) -> bool:
    for callsign, target_path in task.targets:
        try:
            materialize_target(source_path, target_path)
            replacements[(task.show_key, callsign)] = cdn_url(callsign, target_path)
        except OSError as error:
            errors.append(ErrorDetail(callsign, task.source_url, f"Could not save logo: {error}"))
    return task.canonical_path.is_file()


def record_download_success(
    task: DownloadTask,
    replacements: dict[tuple[str, str], str],
    errors: list[ErrorDetail],
    url_cache_updates: list[tuple[str, str, str]],
    show_cache_updates: list[tuple[str, str, str, str, str]],
) -> int:
    failed_shows = 0
    url_cache_updates.append((task.source_url, relative_to_root(task.canonical_path), "done"))
    for show in task.shows:
        if make_show_available(show, task.canonical_path, replacements, errors):
            show_cache_updates.append(
                (
                    show.show_key,
                    show.show_name,
                    show.source_url,
                    relative_to_root(show.canonical_path),
                    "done",
                )
            )
        else:
            failed_shows += 1
            show_cache_updates.append(
                (
                    show.show_key,
                    show.show_name,
                    show.source_url,
                    relative_to_root(show.canonical_path),
                    "failed",
                )
            )
    return failed_shows


def record_final_failure(
    task: DownloadTask,
    reason: str,
    replacements: dict[tuple[str, str], str],
    errors: list[ErrorDetail],
    url_cache_updates: list[tuple[str, str, str]],
    show_cache_updates: list[tuple[str, str, str, str, str]],
) -> int:
    url_cache_updates.append((task.source_url, relative_to_root(task.canonical_path), "failed"))
    for show in task.shows:
        show_cache_updates.append(
            (
                show.show_key,
                show.show_name,
                show.source_url,
                relative_to_root(show.canonical_path),
                "failed",
            )
        )
        for callsign, _ in show.targets:
            replacements[(show.show_key, callsign)] = FALLBACK_LOGO_URL
        errors.append(ErrorDetail(show.canonical_callsign, show.source_url, reason))
    return len(task.shows)


def write_json_atomic(destination: Path, payload: dict[str, Any]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(payload, temporary_file, ensure_ascii=False, separators=(",", ":"))
            temporary_file.write("\n")
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def update_schedules(
    replacements: dict[tuple[str, str], str], errors: list[ErrorDetail]
) -> tuple[int, int]:
    updated_files = 0
    updated_references = 0
    if not replacements:
        return updated_files, updated_references

    for json_path in iter_schedule_files():
        callsign = json_path.parent.name
        try:
            payload = load_json(json_path)
        except LogoError:
            continue
        schedule = payload.get("schedule")
        if not isinstance(schedule, list):
            continue

        changed = False
        for event in schedule:
            if not isinstance(event, dict):
                continue
            show_name = event.get("n")
            logo_url = event.get("l")
            if not isinstance(show_name, str) or not show_name.strip() or not isinstance(logo_url, str):
                continue
            if is_managed_logo_url(logo_url.strip()):
                continue
            replacement = replacements.get((normalized_show_key(show_name), callsign))
            if replacement and logo_url != replacement:
                event["l"] = replacement
                changed = True
                updated_references += 1

        if changed:
            try:
                write_json_atomic(json_path, payload)
                updated_files += 1
            except OSError as error:
                errors.append(ErrorDetail(callsign, str(json_path), f"Could not update JSON: {error}"))
    return updated_files, updated_references


def write_error_file(errors: list[ErrorDetail]) -> None:
    ERROR_FILE.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_FILE.open("w", encoding="utf-8") as error_file:
        for error in errors:
            error_file.write(f"{error.callsign}\n")
            error_file.write(f"URL: {error.url}\n")
            error_file.write(f"ERROR: {error.reason}\n\n")


def main() -> int:
    errors: list[ErrorDetail] = []
    print("TitanTV Logo Downloader\n")
    print("Scanning schedules...\n")

    try:
        shows, source_urls, scan_stats = scan_schedules(errors)
    except LogoError as error:
        errors.append(ErrorDetail("schedule", str(SCHEDULE_DIR), str(error)))
        write_error_file(errors)
        print(f"Error: {error}", file=sys.stderr)
        return 1

    show_tasks = build_tasks(shows)
    print(f"JSON files:\n{scan_stats.json_files}\n")
    print(f"Logo references:\n{scan_stats.logo_references}\n")
    print(f"Unique source URLs:\n{len(source_urls)}\n")
    print(f"Unique show titles:\n{len(show_tasks)}")

    connection = open_cache()
    try:
        show_cache_rows = read_show_cache_rows(connection, [task.show_key for task in show_tasks])
        url_cache_rows = read_url_cache_rows(connection, source_urls)
        replacements: dict[tuple[str, str], str] = {}
        show_cache_updates: list[tuple[str, str, str, str, str]] = []
        url_cache_updates: list[tuple[str, str, str]] = []
        pending_shows: list[ShowTask] = []
        cached_count = 0
        downloaded_count = 0
        failed_count = 0

        for task in show_tasks:
            cached_show = show_cache_rows.get(task.show_key)
            cached_file = cache_path(cached_show[1]) if cached_show else None
            if cached_show and cached_show[2] == "done" and cached_file and cached_file.is_file():
                cached_count += 1
                make_show_available(task, cached_file, replacements, errors)
            else:
                pending_shows.append(task)

        pending_by_url: dict[str, list[ShowTask]] = {}
        for task in pending_shows:
            cached_url = url_cache_rows.get(task.source_url)
            cached_file = cache_path(cached_url[0]) if cached_url else None
            if cached_url and cached_url[1] == "done" and cached_file and cached_file.is_file():
                cached_count += 1
                if make_show_available(task, cached_file, replacements, errors):
                    show_cache_updates.append(
                        (
                            task.show_key,
                            task.show_name,
                            task.source_url,
                            relative_to_root(task.canonical_path),
                            "done",
                        )
                    )
                continue
            pending_by_url.setdefault(task.source_url, []).append(task)

        download_queue: list[DownloadTask] = []
        for source_url, grouped_shows in pending_by_url.items():
            first_show = grouped_shows[0]
            download_queue.append(
                DownloadTask(
                    source_url=source_url,
                    canonical_callsign=first_show.canonical_callsign,
                    canonical_path=first_show.canonical_path,
                    shows=tuple(grouped_shows),
                )
            )

        print("\nCache:")
        print(f"Already downloaded:\n{cached_count}\n")
        print(f"Need download:\n{len(download_queue)}")

        if download_queue and Image is None:
            errors.append(ErrorDetail("setup", "Pillow", "Install with: python3 -m pip install Pillow"))
            write_error_file(errors)
            print("\nError: Pillow is required. Run: python3 -m pip install Pillow", file=sys.stderr)
            return 1

        if download_queue:
            print("\nDownloading...\n")
            worker_count = min(config.DOWNLOAD_WORKERS, len(download_queue))
            retry_queue: list[DownloadTask] = []
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {executor.submit(download_task, task): task for task in download_queue}
                for completed_count, future in enumerate(as_completed(futures), start=1):
                    result = future.result()
                    download_task_result = result.task
                    display_name = f"{safe_component(download_task_result.canonical_callsign)}/{download_task_result.canonical_path.name}"
                    if not result.success:
                        retry_queue.append(download_task_result)
                        print(f"[{completed_count}/{len(download_queue)}] {display_name} RETRY")
                        continue

                    downloaded_count += 1
                    failed_count += record_download_success(
                        download_task_result,
                        replacements,
                        errors,
                        url_cache_updates,
                        show_cache_updates,
                    )
                    print(f"[{completed_count}/{len(download_queue)}] {display_name} OK")

            if retry_queue:
                retry_workers = min(config.FAILED_RETRY_WORKERS, len(retry_queue))
                print(f"\nRetrying {len(retry_queue)} failed logos with {retry_workers} workers...\n")
                with ThreadPoolExecutor(max_workers=retry_workers) as executor:
                    futures = {executor.submit(download_task, task): task for task in retry_queue}
                    for completed_count, future in enumerate(as_completed(futures), start=1):
                        result = future.result()
                        download_task_result = result.task
                        display_name = f"{safe_component(download_task_result.canonical_callsign)}/{download_task_result.canonical_path.name}"
                        if result.success:
                            downloaded_count += 1
                            failed_count += record_download_success(
                                download_task_result,
                                replacements,
                                errors,
                                url_cache_updates,
                                show_cache_updates,
                            )
                            print(f"[{completed_count}/{len(retry_queue)}] {display_name} OK")
                        else:
                            failed_count += record_final_failure(
                                download_task_result,
                                result.reason,
                                replacements,
                                errors,
                                url_cache_updates,
                                show_cache_updates,
                            )
                            print(f"[{completed_count}/{len(retry_queue)}] {display_name} FALLBACK")

        if url_cache_updates:
            upsert_url_cache_rows(connection, url_cache_updates)
        if show_cache_updates:
            upsert_show_cache_rows(connection, show_cache_updates)
        if url_cache_updates or show_cache_updates:
            connection.commit()
    finally:
        connection.close()

    print("\nUpdating JSON files...\n")
    updated_files, updated_references = update_schedules(replacements, errors)
    write_error_file(errors)

    print("Completed\n")
    print(f"Downloaded:\n{downloaded_count}\n")
    print(f"Skipped:\n{cached_count}\n")
    print(f"Failed:\n{failed_count}\n")
    print(f"JSON files updated:\n{updated_files}")
    print(f"Logo references updated:\n{updated_references}\n")
    print(f"Errors:\n{relative_to_root(ERROR_FILE)}")
    return 1 if failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
