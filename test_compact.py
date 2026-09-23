from typing import Any

def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    genre = event.get("displayGenre")
    prog_type = event.get("programType")
    c_val = "/".join(str(x) for x in [genre, prog_type] if x)

    season = event.get("seasonNumber")
    episode = event.get("seasonEpisodeNumber")
    if season not in (None, "") and episode not in (None, ""):
        ep_val = f"S{season}E{episode}"
    else:
        ep_val = ""

    return {
        "n": event.get("title") or "",
        "l": event.get("showCard") or "",
        "s": event.get("startTime") or "",
        "e": event.get("endTime") or "",
        "c": c_val,
        "t": event.get("episodeTitle") or "",
        "d": event.get("description") or "",
        "ep": ep_val,
        "ly": event.get("year") or "",
    }

print(compact_event({
    "displayGenre": "Drama",
    "programType": "Series",
    "seasonNumber": 7,
    "seasonEpisodeNumber": 25,
    "year": 1964
}))
print(compact_event({
    "displayGenre": "Drama",
}))
