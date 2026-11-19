# Competitor Newsroom

One page that pulls news from every tracked company's newsroom, plus the tracked assets' clinical trials, updated every 15 minutes.

- **News** tab: one feed with filters for program, list, priority, company, date and free-text search. NEW badges, unread-only view, mark all read, CSV export. Trial numbers (NCT…) in news summaries link to ClinicalTrials.gov.
- **Trials** tab: every ClinicalTrials.gov trial for the tracked assets, with phase, status, sponsor and primary completion date. NEW marks trials first listed, or whose status, phase or primary completion date changed, since you last looked. Filters for asset, priority, status, phase, change date and breast cancer only. CSV export.
- **Companies** tab: the tracked company list with assets, keyword filters and links to each asset's ClinicalTrials.gov search.
- **Sources** tab: how each company's news is collected and whether its feed is working, plus the status of each ClinicalTrials.gov search.

## How it works

```
companies.yaml  ──►  scripts/fetch.py (GitHub Action, every 15 min)  ──►  data/news.json + data/trials.json  ──►  index.html (GitHub Pages)
```

For each company the fetcher uses the best source it can find:

1. A feed URL set by hand in `companies.yaml` (`rss:`)
2. The newsroom page itself (`scrape:`), for newsrooms with no RSS feed
3. The newsroom's own RSS/Atom feed, found on the newsroom page or the homepage
4. Common feed locations (`/feed`, `/rss`, and the usual Q4 and GCS investor-site feeds)
5. Google News RSS restricted to the company's own website (`site:company.com`), for newsrooms without RSS
6. A Google News search on the company name, for small companies whose site isn't indexed or with no website listed

Large, diversified companies have a `keywords:` list, so only items that mention the tracked assets or indication get through.

Each asset in the `assets:` section is searched on ClinicalTrials.gov by its names and codes (`terms:`).

## One-time setup

1. Create a **new** public repository (for example `competitor-newsroom-2`).
2. Add the files. The GitHub web uploader flattens folders, so create the two nested files with **Add file → Create new file** and type the full path as the name:
   - `scripts/fetch.py`
   - `.github/workflows/update.yml`
   Upload or create the rest at the top level: `index.html`, `companies.yaml`, `requirements.txt`, `.gitignore`, `README.md`. The `data/` folder is created by the first run.
3. **Settings → Actions → General → Workflow permissions:** select **Read and write permissions**, then click Save.
4. **Settings → Pages → Source:** select **GitHub Actions**.
5. **Actions** tab: enable workflows if asked. Then **Update competitor news → Run workflow**. The first run takes about 3–5 minutes and backfills recent news and the current trial list, which aren't marked NEW.
6. The site address is shown in **Settings → Pages**.

The page has a `noindex` tag, so search engines are asked not to list it. Anyone who has the link can still open it.

## Day-to-day

| Task | How |
|---|---|
| Add or remove a company | Edit `companies.yaml` in GitHub and commit. The site updates within a few minutes. |
| Add an asset to the Trials tab | Add a line to the `assets:` section with its names and codes in `terms:`. |
| A feed is wrong or missing | Check the **Sources** tab. Put the correct feed URL in `rss:`, or the press-release page in `scrape:`. |
| Too much noise from a big company | Add or tighten its `keywords:` list, or add words to `exclude:`. |
| Force a refresh now | Actions → Update competitor news → Run workflow |

## Notes

- **Speed:** GitHub runs scheduled jobs every 15 minutes, but can delay them by a few minutes when it's busy. The page checks for new data every 5 minutes. Google News feeds usually lag the newsroom by 15 to 60 minutes. ClinicalTrials.gov records usually change a few days after the sponsor submits an update.
- **Scraped newsrooms** without dates on the list page show the date the item was first picked up.
- **Read/unread** status is kept in each person's own browser. It isn't shared across the team.
- News history is kept for 12 months in `data/news.json`. Trial change history (last 10 changes per trial) is kept in `data/trials.json`.
