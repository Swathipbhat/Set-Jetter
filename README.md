# SetJetter

Name three films you love; an agent finds where in the world feels like them.

**Agent loop** (`agent.py`): Claude (Messages API tool use) calls three Qloo-backed tools:
1. `resolve_movie` - `GET /search` (type `urn:entity:movie`) -> entity id + taste tags (the film "DNA")
2. `find_destinations` - `GET /v2/insights` `filter.type=urn:entity:destination`, `signal.interests.entities=<films>` -> cities by cross-domain taste affinity
3. `find_places` - `GET /v2/insights` `filter.type=urn:entity:place`, `filter.location.query=<city>`, same signal -> hotels / cafes / hotspots

**Why Qloo matters:** destinations and venues come only from Qloo's taste graph. A grounding check
(`_ground`) deletes any city or venue the model names that no Qloo call returned, so the LLM can't
substitute generic travel-blog answers.

## Features
- **Vibe dial**: a 0-100 weight per film. Each signal is queried on its own and the cities are combined by weighted affinity (0 = ignore that film). No LLM needed.
- **Music / food inputs**: optional favorite artist and restaurant are resolved in Qloo and used as extra taste signals.
- **Trip filters**: 1-7 days and a budget ($ / $$ / $$$, sent to Qloo as a price-level filter for hotels and cafes; if Qloo rejects it, the filter is dropped and noted in the trace).
- **Day-by-day itinerary**: built only from venues Qloo returned (coffee, afternoon and evening spots per day).
- **Map**: Leaflet + OpenStreetMap, loaded from unpkg.com, so it needs internet in the browser. Venues without coordinates in Qloo's response can't be pinned.
- **Wardrobe**: `find_fashion` runs once per film (within your budget, using Qloo's price level) so you can dress like each film; brand cards use Qloo's images, with a lettered placeholder when Qloo has none.
- **Workspace layout**: city tabs, then Itinerary / Stay & eat / Wardrobe tabs with a day switcher, a map pinned beside them, and the agent trace in a slide-out drawer, so there is no long scrolling page.
- **Share**: copy a link that re-runs the same query, download the plan as Markdown, or print.

## Speed
A search needs ~30 Qloo calls. They are not run one after another:
- inputs are resolved in parallel, destination queries per film run in parallel, and every venue and outfit lookup for all cities goes out as one parallel batch;
- Qloo options the server rejects (HTTP 400/404/422) are remembered, so later calls start from a request shape that works instead of retrying fallbacks every time;
- successful responses are cached for 15 minutes (repeat searches and shared links are instant), and connections are reused;
- the unused `feature.explainability` flag was removed (it doubled every call if the server rejected it);
- timeouts are 4s connect / 15s read (`QLOO_CONNECT_TIMEOUT`, `QLOO_READ_TIMEOUT`).
Each tool result in the trace drawer shows how many seconds it took. Against a mock server with 0.8s latency per call, one search went from 29.8s / 99 calls to 3.3s / 31 calls.

## Deploy
See `DEPLOY.md` for Render (config in `render.yaml`; the key is set in the Render dashboard, never in the repo).

## Run
```
export QLOO_API_KEY=hack_...            # hackathon key
export ANTHROPIC_API_KEY=sk-ant-...     # optional; without it a scripted planner uses the same Qloo tools
python3 server.py                       # http://localhost:5000
```
Deps: `flask`, `requests`. Optional: `SETJETTER_MODEL`, `QLOO_BASE_URL` (default `https://hackathon.api.qloo.com`).

`QLOO_FIXTURES=1` serves clearly-labelled offline sample data (UI shows a warning) for development without network access.

## Status
Built in a sandbox that could not reach `hackathon.api.qloo.com`, so the live Qloo calls are written against
the documented v2 API but **not yet verified against the live server**. First thing to check on a networked
machine: response shape in `qloo._slim`, and the `filter.tags` values in `qloo.CATEGORIES` (and the fashion tag `urn:tag:genre:brand:fashion` in `find_fashion`) (there is a fallback
that classifies venues locally if the tag filter errors or returns nothing).
