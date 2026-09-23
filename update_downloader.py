import re

with open("logo_downloader.py", "r") as f:
    code = f.read()

# We need to change make_show_available to not require source_path when source_path is None
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
            # Assume it's already on the VPS, just generate the URL
            replacements[(task.show_key, callsign)] = cdn_url(callsign, target_path)
    return True if source_path is None else task.canonical_path.is_file()"""
)

# And in main(), when checking the cache, we don't need cached_file.is_file()
code = code.replace(
"""        for task in show_tasks:
            cached_show = show_cache_rows.get(task.show_key)
            cached_file = cache_path(cached_show[1]) if cached_show else None
            if cached_show and cached_show[2] == "done" and cached_file and cached_file.is_file():
                cached_count += 1
                make_show_available(task, cached_file, replacements, errors)
            else:
                pending_shows.append(task)""",
"""        for task in show_tasks:
            cached_show = show_cache_rows.get(task.show_key)
            if cached_show and cached_show[2] == "done":
                cached_count += 1
                # None as source_path means we trust the database and skip local file materialization
                make_show_available(task, None, replacements, errors)
            else:
                pending_shows.append(task)"""
)

code = code.replace(
"""        for task in pending_shows:
            cached_url = url_cache_rows.get(task.source_url)
            cached_file = cache_path(cached_url[0]) if cached_url else None
            if cached_url and cached_url[1] == "done" and cached_file and cached_file.is_file():
                cached_count += 1
                if make_show_available(task, cached_file, replacements, errors):""",
"""        for task in pending_shows:
            cached_url = url_cache_rows.get(task.source_url)
            if cached_url and cached_url[1] == "done":
                cached_count += 1
                if make_show_available(task, None, replacements, errors):"""
)

with open("logo_downloader.py", "w") as f:
    f.write(code)

