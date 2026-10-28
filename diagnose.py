"""Run this on a machine that can reach Qloo:  QLOO_API_KEY=... python3 diagnose.py "Emily in Paris" "In the Mood for Love"
Prints the raw (truncated) Qloo responses so the client in qloo.py can be fixed against real data."""
import json, os, sys, requests
BASE = os.environ.get("QLOO_BASE_URL", "https://hackathon.api.qloo.com")
H = {"X-Api-Key": os.environ["QLOO_API_KEY"]}

def show(label, path, params):
    r = requests.get(BASE + path, params=params, headers=H, timeout=30)
    print(f"\n=== {label}\nGET {r.url}\nHTTP {r.status_code}\n{r.text[:1800]}")
    try:
        return r.json()
    except ValueError:
        return {}

ids = []
for title in sys.argv[1:] or ["In the Mood for Love"]:
    j = show(f"search movie: {title}", "/search", {"query": title, "types": "urn:entity:movie", "take": 2})
    res = j.get("results", j)
    res = res if isinstance(res, list) else res.get("entities", [])
    if res:
        ids.append(res[0].get("entity_id") or res[0].get("id"))
print("\nresolved ids:", ids)
if ids:
    sig = ",".join(ids)
    d = show("destinations", "/v2/insights", {"filter.type": "urn:entity:destination", "signal.interests.entities": sig, "take": 3})
    show("destinations + explainability", "/v2/insights", {"filter.type": "urn:entity:destination", "signal.interests.entities": sig, "take": 3, "feature.explainability": "true"})
    show("places in Paris", "/v2/insights", {"filter.type": "urn:entity:place", "filter.location.query": "Paris", "signal.interests.entities": sig, "take": 3})
    show("hotels in Paris", "/v2/insights", {"filter.type": "urn:entity:place", "filter.location.query": "Paris", "signal.interests.entities": sig, "filter.tags": "urn:tag:category:place:hotel", "take": 3})
