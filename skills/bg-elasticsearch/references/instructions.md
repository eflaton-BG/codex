---
applyTo: "**"
---

# Elasticsearch Query Workflow

When the user asks to query Elasticsearch logs (e.g. "query ES for...", "check logs in...", "summarize activity for..."), follow this workflow.

## Setup

**Venv:** Always activate before running any Python:
```bash
source ~/bg/myenv/bin/activate && python3 - << 'EOF'
...
EOF
```

If the snippet needs `bg_vault_elastic`, prefer the bundled helper:
```bash
bash scripts/run_bg_vault_elastic_python.sh -c 'from bg_vault_elastic.client import VaultElasticClient; print("import ok")'
```

The helper uses:
- `BG_ELASTIC_VENV` when it is set
- `BG_ELASTIC_PYTHON` when it is set
- `BG_VAULT_ELASTIC_DIR` when it is set

If those are not set, the helper falls back to its local defaults. Prefer environment overrides instead of hardcoding host-specific paths into query snippets.

**Config file:** `~/.codex/skills/bg-elasticsearch/references/rsps_bg_agents_es_cfg.json`
- Contains one entry per site with `elastic_url`, `index_alias`, `customer`, `site`
- Select the correct entry by matching `site` (3-letter code) or `customer`

**Credentials:** Use `VaultElasticClient` with the cluster key derived from the customer name:
- Washington -> `"elastic-washington-cluster"`
- Dev/ITF -> `"elastic-dev-cluster"`
- Huron -> `"elastic-huron-cluster"`
- Sunflower -> `"elastic-sunflower-cluster"`
- Britton -> `"elastic-britton-cluster"`
- Maunakea -> `"elastic-maunakea-cluster"`

**ES Client:** Use `elasticsearch` v8 (`pip show elasticsearch` should show 8.x). Use `basic_auth` (not `http_auth`):

```python
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from bg_vault_elastic.client import VaultElasticClient
from elasticsearch import Elasticsearch

with Path("~/.codex/skills/bg-elasticsearch/references/rsps_bg_agents_es_cfg.json").expanduser().open() as f:
    configs = json.load(f)

cfg = next(c for c in configs if c["site"] == "SITE_CODE")

client = VaultElasticClient()
creds = client.get_es_credentials("elastic-CUSTOMER-cluster")

es = Elasticsearch(hosts=cfg["elastic_url"], basic_auth=(creds["username"], creds["password"]))

now = datetime.now(timezone.utc)
since = now - timedelta(minutes=15)

query = {
    "query": {
        "bool": {
            "must": [
                {"match": {"field": "value"}},
                {"range": {"@timestamp": {"gte": since.isoformat(), "lte": now.isoformat()}}}
            ]
        }
    },
    "size": 500,
    "sort": [{"@timestamp": {"order": "asc"}}]
}

result = es.search(index=cfg["index_alias"], body=query)
hits = result["hits"]["hits"]
print(f"Total hits: {result['hits']['total']['value']}")
for hit in hits:
    src = hit["_source"]
    print(f"{src.get('@timestamp')} | {src.get('level', '')} | {src.get('message', '')}")
```

## Workflow

1. **Confirm before running** -- show the user the site/index, query filters, and time range before executing
2. **Select config entry** by `site` or `customer` from the config file
3. **Use `requests`** (not the `Elasticsearch` client) due to version incompatibility
4. **Summarize results** -- after fetching hits, group by log level or message pattern and give a human-readable summary
