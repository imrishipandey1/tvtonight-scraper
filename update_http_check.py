import re

with open("logo_downloader.py", "r") as f:
    code = f.read()

# Add a function to check URL concurrently
check_func = """
def is_url_accessible(url: str) -> bool:
    try:
        req = Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=5) as response:
            return response.status >= 200 and response.status < 400
    except Exception:
        return False
"""
code = code.replace("def make_show_available", check_func + "\ndef make_show_available")

# Replace the sequential cache check with a concurrent one
old_check = """
        for task in show_tasks:
            cached_show = show_cache_rows.get(task.show_key)
            if cached_show and cached_show[2] == "done":
                cached_count += 1
                # None as source_path means we trust the database and skip local file materialization
                make_show_available(task, None, replacements, errors)
            else:
                pending_shows.append(task)
"""

new_check = """
        print("\\nVerifying cached images on VPS...", flush=True)
        # Verify cached shows concurrently using HTTP HEAD
        def verify_task(task):
            cached_show = show_cache_rows.get(task.show_key)
            if cached_show and cached_show[2] == "done":
                expected_url = cdn_url(task.targets[0][0], task.targets[0][1])
                if is_url_accessible(expected_url):
                    return task, True
            return task, False

        with ThreadPoolExecutor(max_workers=50) as executor:
            for task, is_valid in executor.map(verify_task, show_tasks):
                if is_valid:
                    cached_count += 1
                    make_show_available(task, None, replacements, errors)
                else:
                    pending_shows.append(task)
"""
code = code.replace(old_check, new_check)

old_pending = """
        for task in pending_shows:
            cached_url = url_cache_rows.get(task.source_url)
            if cached_url and cached_url[1] == "done":
                cached_count += 1
                if make_show_available(task, None, replacements, errors):
                    show_cache_updates.append(
"""

new_pending = """
        final_pending_shows = []
        def verify_pending(task):
            cached_url = url_cache_rows.get(task.source_url)
            if cached_url and cached_url[1] == "done":
                expected_url = cdn_url(task.targets[0][0], task.targets[0][1])
                if is_url_accessible(expected_url):
                    return task, True
            return task, False

        with ThreadPoolExecutor(max_workers=50) as executor:
            for task, is_valid in executor.map(verify_pending, pending_shows):
                if is_valid:
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
                else:
                    final_pending_shows.append(task)
"""
# Replace carefully
code = code.replace(old_pending, new_pending)

# We also need to change pending_by_url to use final_pending_shows
code = code.replace("        for task in pending_shows:\n            cached_url", "        # Replaced pending_shows logic above")
code = code.replace("pending_by_url: dict[str, list[ShowTask]] = {}\n        for task in pending_shows:", "pending_by_url: dict[str, list[ShowTask]] = {}\n        for task in final_pending_shows:")

with open("logo_downloader.py", "w") as f:
    f.write(code)

