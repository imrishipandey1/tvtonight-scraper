#!/usr/bin/env python3
"""Download seven days of TitanTV schedules for callsigns listed in data/*.txt."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
LINEUPS_FILE = ROOT_DIR / "lineups.txt"
SCHEDULE_DIR = ROOT_DIR / "schedule"
ERRORS_FILE = ROOT_DIR / "errors.jsonl"

# Set this once for your TitanTV account. --user-id can override it when needed.
USER_ID = "dd77a9d6-ad6d-453e-9a94-719f180363da"
DEFAULT_BASE_URL = "https://www.titantv.com"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_RETRIES = 3
DEFAULT_WORKERS = 20


class ScraperError(Exception):
    """An expected error that should be recorded and allow the run to continue."""


@dataclass(frozen=True)
class LineupInput:
    key: str
    titan_name: str
    callsigns: tuple[str, ...]


@dataclass(frozen=True)
class ScheduleJob:
    lineup_name: str
    lineup_id: str
    callsign: str
    channel_name: str
    channel_index: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download TitanTV schedules for callsigns in data/*.txt."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="TitanTV site URL")
    parser.add_argument("--user-id", default=USER_ID, help="TitanTV user ID")
    parser.add_argument(
        "--start-date",
        help="First date to download (YYYY-MM-DD). Defaults to today's local date.",
    )
    parser.add_argument("--days", type=int, default=7, help="Days per request (default: 7)")
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="Concurrent schedule requests (default: 6)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Attempts for each HTTP request (default: 3)",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> date:
    if not args.user_id or args.user_id.startswith("YOUR_"):
        raise ScraperError("Set USER_ID in titantv_scraper.py or pass --user-id.")
    if args.days < 1 or args.days > 14:
        raise ScraperError("--days must be between 1 and 14.")
    if args.workers < 1 or args.workers > 20:
        raise ScraperError("--workers must be between 1 and 20.")
    if args.timeout < 1:
        raise ScraperError("--timeout must be at least 1 second.")
    if args.retries < 1:
        raise ScraperError("--retries must be at least 1.")

    if not args.start_date:
        return datetime.now().astimezone().date()
    try:
        return date.fromisoformat(args.start_date)
    except ValueError as error:
        raise ScraperError("--start-date must use YYYY-MM-DD.") from error


def append_errors(errors: list[dict[str, Any]]) -> None:
    if not errors:
        return
    with ERRORS_FILE.open("a", encoding="utf-8") as error_file:
        for error in errors:
            error_file.write(json.dumps(error, ensure_ascii=False, separators=(",", ":")))
            error_file.write("\n")


def error_record(stage: str, message: str, **context: Any) -> dict[str, Any]:
    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "stage": stage,
        "message": message,
        **context,
    }


def request_json(url: str, timeout: int, retries: int) -> Any:
    headers = {
        "Accept": "application/json",
        "Referer": DEFAULT_BASE_URL + "/",
        "User-Agent": "TitanTVLocalScheduleDownloader/1.0",
    }
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", response.getcode())
                if status < 200 or status >= 300:
                    raise ScraperError(f"HTTP {status}")
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ScraperError) as error:
            last_error = error
            if attempt < retries:
                retry_after = error.headers.get("Retry-After") if isinstance(error, HTTPError) else None
                try:
                    delay = min(int(retry_after), 30) if retry_after else min(2 ** (attempt - 1), 8)
                except ValueError:
                    delay = min(2 ** (attempt - 1), 8)
                time.sleep(delay)

    raise ScraperError(f"Request failed after {retries} attempts: {last_error}")


def path_url(base_url: str, *parts: str) -> str:
    quoted_parts = "/".join(quote(str(part), safe="") for part in parts)
    return f"{base_url.rstrip('/')}/{quoted_parts}"


def list_from_payload(payload: Any, keys: tuple[str, ...], endpoint: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise ScraperError(f"Unexpected JSON structure from {endpoint}.")


def first_nonempty(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_callsign(value: str) -> str:
    return value.strip().upper()


def safe_component(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    safe_value = re.sub(r"[^A-Za-z0-9._ -]+", "-", normalized).strip(" .-")
    safe_value = re.sub(r"\s+", " ", safe_value)
    if not safe_value or safe_value in {".", ".."}:
        return "unnamed"
    return safe_value[:120]


def read_lineup_mapping() -> dict[str, str]:
    if not LINEUPS_FILE.is_file():
        raise ScraperError(f"Missing mapping file: {LINEUPS_FILE.name}")

    mappings: dict[str, str] = {}
    for line_number, raw_line in enumerate(LINEUPS_FILE.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            raise ScraperError(f"{LINEUPS_FILE.name}:{line_number} must be file-name|TitanTV lineupName")
        key, titan_name = (part.strip() for part in line.split("|", 1))
        if not key or not titan_name:
            raise ScraperError(f"{LINEUPS_FILE.name}:{line_number} has an empty value")
        if key in mappings:
            raise ScraperError(f"{LINEUPS_FILE.name}:{line_number} duplicates '{key}'")
        mappings[key] = titan_name
    return mappings


def read_callsigns(channel_file: Path) -> tuple[str, ...]:
    callsigns: list[str] = []
    seen: set[str] = set()
    for raw_line in channel_file.read_text(encoding="utf-8").splitlines():
        callsign = raw_line.strip()
        if not callsign or callsign.startswith("#"):
            continue
        normalized = normalize_callsign(callsign)
        if normalized not in seen:
            callsigns.append(callsign)
            seen.add(normalized)
    return tuple(callsigns)


def discover_inputs(mappings: dict[str, str], errors: list[dict[str, Any]]) -> list[LineupInput]:
    if not DATA_DIR.is_dir():
        raise ScraperError(f"Missing data directory: {DATA_DIR.name}/")

    inputs: list[LineupInput] = []
    for channel_file in sorted(DATA_DIR.glob("*.txt")):
        key = channel_file.stem
        titan_name = mappings.get(key)
        if not titan_name:
            errors.append(
                error_record("input", "No matching lineup mapping", file=str(channel_file), lineup_key=key)
            )
            continue
        callsigns = read_callsigns(channel_file)
        if not callsigns:
            errors.append(error_record("input", "No callsigns found", file=str(channel_file)))
            continue
        inputs.append(LineupInput(key=key, titan_name=titan_name, callsigns=callsigns))
    if not inputs:
        raise ScraperError("No usable .txt channel files found in data/.")
    return inputs


def get_lineup_id(lineups: list[dict[str, Any]], lineup_name: str) -> tuple[str, str]:
    for lineup in lineups:
        current_name = first_nonempty(lineup, ("lineupName", "name"))
        if current_name != lineup_name:
            continue
        lineup_id = first_nonempty(lineup, ("lineupId", "id", "lineupCacheId"))
        if lineup_id:
            return lineup_id, current_name
        raise ScraperError(f"Lineup '{lineup_name}' has no lineup ID in the API response.")
    raise ScraperError(f"TitanTV lineup '{lineup_name}' was not found for this user ID.")


def channel_lookup(channels: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for channel in channels:
        callsign = first_nonempty(channel, ("callSign", "callsign", "call_sign"))
        channel_index = channel.get("channelIndex")
        if callsign and channel_index is not None:
            lookup.setdefault(normalize_callsign(callsign), channel)
    return lookup


def channel_label(channel: dict[str, Any], fallback: str) -> str:
    return first_nonempty(
        channel,
        ("channelName", "displayName", "name", "channelDisplayName", "callSign"),
    ) or fallback


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "n": event.get("title") or "",
        "l": event.get("showCard") or "",
        "s": event.get("startTime") or "",
        "e": event.get("endTime") or "",
        "c": event.get("displayGenre") or "",
        "t": event.get("episodeTitle") or "",
        "d": event.get("description") or "",
    }


def write_json_atomic(destination: Path, payload: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary_file:
        json.dump(payload, temporary_file, ensure_ascii=False, separators=(",", ":"))
        temporary_file.write("\n")
        temporary_name = temporary_file.name
    os.replace(temporary_name, destination)


def download_schedule(
    job: ScheduleJob,
    *,
    base_url: str,
    user_id: str,
    start_date: date,
    days: int,
    timeout: int,
    retries: int,
) -> int:
    print(f"Scraping: {job.lineup_name} / {job.callsign}", flush=True)
    start = start_date.strftime("%Y%m%d")
    endpoint = path_url(
        base_url,
        "dailygrid",
        "api",
        "schedule",
        user_id,
        job.lineup_id,
        f"{start}0000",
        "1440",
        str(days),
    )
    url = f"{endpoint}?{urlencode({'channelIndex': job.channel_index, 'channelCount': 1})}"
    payload = request_json(url, timeout, retries)
    schedule_channels = list_from_payload(payload, ("channels",), "schedule API")
    matching_channel = next(
        (
            channel
            for channel in schedule_channels
            if str(channel.get("channelIndex", "")) == str(job.channel_index)
        ),
        schedule_channels[0] if schedule_channels else None,
    )
    if not matching_channel:
        raise ScraperError("Schedule response contains no channel data.")

    raw_days = matching_channel.get("days")
    if not isinstance(raw_days, list):
        raise ScraperError("Schedule response contains no days.")

    events_by_day: dict[int, list[dict[str, Any]]] = {}
    for raw_day in raw_days:
        if not isinstance(raw_day, dict):
            continue
        try:
            day_number = int(raw_day.get("dayNumber"))
        except (TypeError, ValueError):
            continue
        if day_number < 0 or day_number >= days:
            continue
        raw_events = raw_day.get("events")
        events_by_day[day_number] = [
            compact_event(event) for event in raw_events if isinstance(event, dict)
        ] if isinstance(raw_events, list) else []

    output_lineup = safe_component(job.lineup_name)
    output_callsign = safe_component(job.callsign)
    for day_number in range(days):
        schedule_date = start_date + timedelta(days=day_number)
        destination = SCHEDULE_DIR / output_lineup / output_callsign / f"{schedule_date.isoformat()}.json"
        write_json_atomic(
            destination,
            {
                "channel": job.channel_name,
                "date": schedule_date.isoformat(),
                "schedule": events_by_day.get(day_number, []),
            },
        )
    return days


def remove_expired_schedule_files(start_date: date, errors: list[dict[str, Any]]) -> int:
    if not SCHEDULE_DIR.is_dir():
        return 0

    removed_files = 0
    for schedule_file in SCHEDULE_DIR.rglob("*.json"):
        try:
            schedule_date = date.fromisoformat(schedule_file.stem)
        except ValueError:
            continue
        if schedule_date >= start_date:
            continue
        try:
            schedule_file.unlink()
            removed_files += 1
        except OSError as error:
            errors.append(
                error_record(
                    "cleanup",
                    f"Could not remove expired schedule file: {error}",
                    file=str(schedule_file),
                )
            )
    return removed_files


def main() -> int:
    args = parse_args()
    errors: list[dict[str, Any]] = []
    try:
        start_date = validate_args(args)
        mappings = read_lineup_mapping()
        lineup_inputs = discover_inputs(mappings, errors)

        print(f"Fetching TitanTV lineups for {len(lineup_inputs)} channel lists...", flush=True)
        lineup_url = path_url(args.base_url, "api", "lineup", args.user_id)
        lineup_payload = request_json(lineup_url, args.timeout, args.retries)
        available_lineups = list_from_payload(lineup_payload, ("lineups", "data", "items"), "lineup API")
        print(f"Found {len(available_lineups)} available lineups.", flush=True)

        jobs: list[ScheduleJob] = []
        loaded_channels: dict[str, dict[str, dict[str, Any]]] = {}
        resolved_lineups: dict[str, tuple[str, str]] = {}
        seen_jobs: set[tuple[str, str]] = set()

        for lineup_input in lineup_inputs:
            try:
                print(f"Preparing: {lineup_input.key} ({lineup_input.titan_name})", flush=True)
                if lineup_input.titan_name not in resolved_lineups:
                    resolved_lineups[lineup_input.titan_name] = get_lineup_id(
                        available_lineups, lineup_input.titan_name
                    )
                lineup_id, resolved_name = resolved_lineups[lineup_input.titan_name]
                if lineup_id not in loaded_channels:
                    print(f"Loading channels: {resolved_name}", flush=True)
                    channels_url = path_url(args.base_url, "api", "channel", args.user_id, lineup_id)
                    channels_payload = request_json(channels_url, args.timeout, args.retries)
                    channels = list_from_payload(channels_payload, ("channels", "data", "items"), "channel API")
                    loaded_channels[lineup_id] = channel_lookup(channels)

                lookup = loaded_channels[lineup_id]
                for callsign in lineup_input.callsigns:
                    channel = lookup.get(normalize_callsign(callsign))
                    if not channel:
                        print(
                            f"Not found: {resolved_name} / {callsign}",
                            file=sys.stderr,
                            flush=True,
                        )
                        errors.append(
                            error_record(
                                "channel-match",
                                "callsign not found in lineup",
                                lineup=resolved_name,
                                lineup_id=lineup_id,
                                callsign=callsign,
                            )
                        )
                        continue
                    job_key = (lineup_id, normalize_callsign(callsign))
                    if job_key in seen_jobs:
                        continue
                    seen_jobs.add(job_key)
                    try:
                        channel_index = int(channel["channelIndex"])
                    except (KeyError, TypeError, ValueError):
                        print(
                            f"No channel index: {resolved_name} / {callsign}",
                            file=sys.stderr,
                            flush=True,
                        )
                        errors.append(
                            error_record(
                                "channel-match",
                                "channel has no valid channelIndex",
                                lineup=resolved_name,
                                callsign=callsign,
                            )
                        )
                        continue
                    jobs.append(
                        ScheduleJob(
                            lineup_name=resolved_name,
                            lineup_id=lineup_id,
                            callsign=callsign,
                            channel_name=channel_label(channel, callsign),
                            channel_index=channel_index,
                        )
                    )
            except ScraperError as error:
                print(
                    f"Failed lineup: {lineup_input.titan_name} — {error}",
                    file=sys.stderr,
                    flush=True,
                )
                errors.append(
                    error_record("lineup", str(error), file_key=lineup_input.key, lineup=lineup_input.titan_name)
                )

        if not jobs:
            raise ScraperError("No channels could be matched. Check lineups.txt and data/*.txt.")

        saved_files = 0
        print(
            f"Scraping {len(jobs)} channels with {min(args.workers, len(jobs))} workers...",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=min(args.workers, len(jobs))) as executor:
            futures = {
                executor.submit(
                    download_schedule,
                    job,
                    base_url=args.base_url,
                    user_id=args.user_id,
                    start_date=start_date,
                    days=args.days,
                    timeout=args.timeout,
                    retries=args.retries,
                ): job
                for job in jobs
            }
            for completed_count, future in enumerate(as_completed(futures), start=1):
                job = futures[future]
                try:
                    saved_days = future.result()
                    saved_files += saved_days
                    print(
                        f"[{completed_count}/{len(jobs)}] Saved: {job.lineup_name} / {job.callsign} ({saved_days} files)",
                        flush=True,
                    )
                except (ScraperError, OSError) as error:
                    print(
                        f"[{completed_count}/{len(jobs)}] Failed: {job.lineup_name} / {job.callsign} — {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    errors.append(
                        error_record(
                            "schedule",
                            str(error),
                            lineup=job.lineup_name,
                            lineup_id=job.lineup_id,
                            callsign=job.callsign,
                            channel_index=job.channel_index,
                        )
                    )

        removed_files = remove_expired_schedule_files(start_date, errors)
        if removed_files:
            print(f"Removed {removed_files} expired schedule files.", flush=True)

        append_errors(errors)
        print(f"Finished: {saved_files} JSON files for {len(jobs)} matched channels.")
        if errors:
            print(f"Warnings/errors: {len(errors)} written to {ERRORS_FILE.name}.", file=sys.stderr)
        return 0
    except (ScraperError, OSError) as error:
        errors.append(error_record("setup", str(error)))
        append_errors(errors)
        print(f"Error: {error}. Details saved to {ERRORS_FILE.name}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
