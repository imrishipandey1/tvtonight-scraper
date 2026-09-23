import re

with open("logo_downloader.py", "r") as f:
    code = f.read()

# We revert make_show_available to allow source_path to be None
code = code.replace(
"""def make_show_available(
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
    return task.canonical_path.is_file()""",
"""def make_show_available(
    task: ShowTask,
    source_path: Path | None,
    replacements: dict[tuple[str, str], str],
    errors: list[ErrorDetail],
) -> bool:
    for callsign, target_path in task.targets:
        if source_path is not None:
            try:
                materialize_target(source_path, target_path)
                replacements[(task.show_key, callsign)] = cdn_url(callsign, target_path)
            except OSError as error:
                errors.append(ErrorDetail(callsign, task.source_url, f"Could not save logo: {error}"))
        else:
            replacements[(task.show_key, callsign)] = cdn_url(callsign, target_path)
    return True if source_path is None else task.canonical_path.is_file()"""
)

# Replace the cache check loop in main() with HTTP HEAD checks
old_loop = """        for task in show_tasks:
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
            pending_by_url.setdefault(task.source_url, []).append(task)"""

new_loop = """
        def is_url_accessible(url: str) -> bool:
            try:
                req = Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(req, timeout=5) as response:
                    return response.status >= 200 and response.status < 400
            except Exception:
                return False

        print("\\nVerifying cached images on VPS...", flush=True)

        for task in show_tasks:
            cached_show = show_cache_rows.get(task.show_key)
            if cached_show and cached_show[2] == "done":
                # Check if it actually exists on the VPS
                expected_url = cdn_url(task.targets[0][0], task.targets[0][1])
                if is_url_accessible(expected_url):
                    cached_count += 1
                    make_show_available(task, None, replacements, errors)
                else:
                    pending_shows.append(task)
            else:
                pending_shows.append(task)

        pending_by_url: dict[str, list[ShowTask]] = {}
        for task in pending_shows:
            cached_url = url_cache_rows.get(task.source_url)
            if cached_url and cached_url[1] == "done":
                expected_url = cdn_url(task.targets[0][0], task.targets[0][1])
                if is_url_accessible(expected_url):
                    cached_count += 1
                    if make_show_available(task, None, replacements, errors):
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
"""
code = code.replace(old_loop, new_loop)

with open("logo_downloader.py", "w") as f:
    f.write(code)

