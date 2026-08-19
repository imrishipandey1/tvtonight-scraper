# TitanTV local schedule scraper

This downloads a 24-hour schedule for seven days for every callsign listed in `data/*.txt`. It uses only the Python standard library, so a normal Mac Python 3.10+ installation is enough.

## Files to create

Create `lineups.txt` beside the script. Each line maps a `data` filename (without `.txt`) to the exact TitanTV `lineupName`:

```text
# local-file-name|exact TitanTV lineupName
directv|DirecTV - National
new-york-city|Broadcast - New York NY
```

Create one or more callsign lists under `data/`. The filename must have a matching first value in `lineups.txt`.

`data/directv.txt`

```text
# One TitanTV callSign per line
CNN
ESPN
HBO
```

`data/new-york-city.txt`

```text
WABC-DT
WCBS-DT
WNBC-DT
```

Blank lines, comments starting with `#`, and repeated callsigns are ignored.

## Run locally on Mac

1. Check the `USER_ID` value near the top of `titantv_scraper.py`. It is fixed there and can be changed once if needed.
2. Create `lineups.txt` and your files in `data/` using the formats above.
3. From this folder, run:

   ```bash
   python3 titantv_scraper.py
   ```

Use a specific first date if needed:

```bash
python3 titantv_scraper.py --start-date 2026-08-19
```

Useful options:

```bash
python3 titantv_scraper.py --workers 4 --timeout 45 --retries 4
python3 titantv_scraper.py --user-id YOUR_TITANTV_USER_ID
python3 titantv_scraper.py --base-url https://www.titantv.com
```

## Output

The scraper first calls these API endpoints:

```text
/api/lineup/{userId}
/api/channel/{userId}/{lineupId}
/dailygrid/api/schedule/{userId}/{lineupId}/{YYYYMMDD}0000/1440/7?channelIndex={channelIndex}&channelCount=1
```

It dynamically obtains each `lineupId` and `channelIndex`; do not save those values in your input files.

Each requested channel creates seven files here (unsafe filename characters are made safe automatically):

```text
schedule/
  DirecTV - National/
    CNN/
      2026-08-19.json
      2026-08-20.json
```

Each JSON file follows this compact schedule structure:

```json
{
  "channel": "WABC-DT New York, N.Y. (ABC)",
  "date": "2026-08-19",
  "schedule": [
    {
      "n": "9-1-1",
      "l": "https://cdn2.titantv.com/show-card.png",
      "s": "2026-08-19T20:00:00",
      "e": "2026-08-19T21:00:00",
      "c": "Drama",
      "t": "Episode title",
      "d": "Episode description"
    }
  ]
}
```

Key mapping: `n` show name, `l` show logo, `s` start time, `e` end time, `c` category, `t` episode title, `d` episode description.

## Errors and retries

The script retries transient HTTP, timeout, and invalid JSON failures with backoff. It continues when a channel or lineup fails, and appends details to `errors.jsonl`. A successful run may still report warnings there for missing callsigns or failed channels.
