# App Store Radar

A small Python monitor for apps across App Store regions. It follows Apple's [new-app sitemap index](https://apps.apple.com/sitemaps_apps_index_new-app_1.xml), discovers app IDs and gradually fills a local SQLite database with Apple Lookup metadata.

## Run

From the repository directory, with Python 3.9 or later:

```sh
python3 monitor_app_store.py
```

Leave it running. Stop with Ctrl+C and run the same command to resume. Shutdown waits for an in-flight sitemap request to finish. The database lives at `data/apps.sqlite3`. Use `--db PATH` to choose another location. Only one monitor should write to a given database at a time.

Collection data is created locally when you run the monitor. The database and generated exports are excluded from Git.

## Collection

- The worker fetches the index at startup and checks it every three hours. Each listed sitemap is also checked on its own three-hour schedule. Conditional HTTP requests reuse Apple's cache validators when available.
- Main and alternate storefront links from all regions count. Each app ID has one record plus the regions observed in the sitemap. New apps use US when listed, otherwise the first region alphabetically. The selected region stays stable as more regions are discovered. First discovery queues one metadata lookup. A changed URL in the selected region or newer per-app sitemap modification date queues a refresh.
- Lookup requests group apps by selected region and contain up to 200 IDs, with at least five seconds between batches. A single background thread downloads and parses sitemaps while the main worker collects metadata and owns database writes.
- Each successful work unit commits independently. A missing Lookup result is retried after a day. Temporary network failures are deferred and a 429 persists the task retry and the global pause together according to Retry-After, with a five-minute fallback. Requests already in flight can finish during the pause.
- Coverage follows the regional entries Apple exposes in this sitemap. The saved region list accumulates observations and can include regions from earlier scans. Prices, ratings and descriptions belong to the selected region. First discovery time and Apple's release date are separate fields. When Apple omits a per-app modification date, edits under an unchanged URL can go undetected.

Existing US-only databases upgrade automatically on the next monitor run. Collected records stay intact. Sitemap cache validators are reset once to discover other regions.

## Export

```sh
python3 monitor_app_store.py export > apps.jsonl
```

The export is a snapshot and can run during collection. Each UTF-8 JSON line represents one app. Fields include:

| Field | Contents |
| --- | --- |
| app_id, url, country | Apple ID, selected storefront URL and its country code |
| regions | Country codes observed for the app in the sitemap |
| first_seen_at, sitemap_lastmod | Local discovery time in UTC and Apple's optional modification date |
| metadata | Complete Lookup record, including release and version dates, category, seller and ratings when supplied |

A null metadata field means the worker has yet to obtain it. For a spreadsheet, select fields such as `metadata.trackName`, `metadata.releaseDate` and `metadata.sellerUrl` when importing the JSONL.

## License

MIT. The license covers the monitor code. Collected listings come from Apple and the respective publishers.
