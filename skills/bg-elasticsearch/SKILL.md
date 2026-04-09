---
name: bg-elasticsearch
description: Connect to a Berkshire Grey Elasticsearch deployment when the user specifies a customer and site name. Uses the mapping of customer, site, index name to index alias and id from https://github.com/berkshiregrey/bg_agents/blob/devel/n8n_workflows/Data%20Tables/Elastic%20Indices.csv.
---

# Elasticsearch Site Lookup

Use this skill when the user wants to query Elasticsearch by customer and site.

## Additional Instructions

Read `references/instructions.md` before running Elasticsearch queries. Follow that file when it conflicts with older examples in this skill.

## Workflow

1. Treat the customer and site as separate concepts.
2. Use the customer, site, program name, and/or data type to find the Elasticsearch connection details from the CSV inventory.
3. If the exact index or alias is still unknown after checking the CSV, ask the user for the connection details. Try search index patterns derived from the normalized customer and site names using patterns from the CSV inventory.
4. Fetch credentials with `VaultElasticClient` when the workflow requires Vault-derived credentials.
5. If host `python3` cannot import `bg_vault_elastic`, use the bundled helper script at `scripts/run_bg_vault_elastic_python.sh` for Python snippets that import it.
6. Use the resolved `.es.` URL for Elasticsearch API calls. Do not use a Kibana `.kb.` URL as the Elasticsearch host.

## Local Environment

If `python3 -c 'import bg_vault_elastic'` fails, use the helper script from this skill directory:

```bash
bash scripts/run_bg_vault_elastic_python.sh -c 'from bg_vault_elastic.client import VaultElasticClient; print("import ok")'
```

The helper script:

- defaults to `BG_ELASTIC_VENV=/home/ezekiel.flaton@berkshiregrey.com/devel/colcon_ws/src/.venv`
- defaults to `BG_ELASTIC_PYTHON=$BG_ELASTIC_VENV/bin/python`
- uses `BG_VAULT_ELASTIC_DIR` when it is set
- otherwise defaults to `$HOME/bg-vault-client/bg_vault_elastic`
- prepends `$BG_VAULT_ELASTIC_DIR/src` to `PYTHONPATH`
- runs the code with the configured venv Python

## Time Field Selection

Do not assume every Elasticsearch dataset uses `@timestamp` as its business time field.

Use these defaults unless the mapping or Kibana data view shows otherwise:

- `picks` / `pick_stats` / `mongospy-pick-stats`: use `date_created`
- `transfers` / `transfer_stats` / `mongospy-transfer-stats`: use `date_created`
- `metrics` / `metric_events`: use `@timestamp`
- `wms`: use `@timestamp`
- `log.records`: use `@timestamp`

When reconciling with Kibana Discover:

- check the data view's configured time field first
- if Kibana is using `date_created`, use `date_created` in Elasticsearch range filters and sorts
- if Kibana is using `@timestamp`, use `@timestamp`

If counts disagree with Kibana, verify the active time field before assuming the alias, site token, or time window is wrong.

## CSV Inventory Lookup

When the user provides business identifiers but not the Elasticsearch URL, alias, or index ID, use the inventory CSV in GitHub as the primary source of truth before probing Vault or Elasticsearch.

Repository details:

- repo: `berkshiregrey/bg_agents`
- branch: `devel`
- path: `n8n_workflows/Data Tables/Elastic Indices.csv`
- blob URL: `https://github.com/berkshiregrey/bg_agents/blob/devel/n8n_workflows/Data%20Tables/Elastic%20Indices.csv`
- raw URL: `https://raw.githubusercontent.com/berkshiregrey/bg_agents/devel/n8n_workflows/Data%20Tables/Elastic%20Indices.csv`

Interpretation rules:

- the first row contains the column headings
- match rows using the user-provided customer, site, program name, and data type
- normalize matches case-insensitively
- trim leading and trailing whitespace
- collapse repeated internal whitespace before comparing when needed
- do not guess if multiple rows still match after normalization; report the ambiguity and list the discriminating fields
- if a single row matches, treat that row's URL, alias, and index identifier as authoritative

Use the CSV-backed values to resolve:

- Elasticsearch URL
- `index_alias`
- `index_id`

If the CSV does not contain a unique match, fall back to the Vault and alias-discovery workflow below.

Example CSV lookup pattern:

```python
import csv
import re
from io import StringIO

import requests


def normalize(value: str) -> str:
    value = value.strip().lower()
    return re.sub(r"\s+", " ", value)


csv_url = "https://raw.githubusercontent.com/berkshiregrey/bg_agents/devel/n8n_workflows/Data%20Tables/Elastic%20Indices.csv"

customer = "Washington"
site = "Pittston"
program = "Example Program"
data_type = "log.records"

resp = requests.get(csv_url, timeout=30)
resp.raise_for_status()

reader = csv.DictReader(StringIO(resp.text))
matches = [
    row
    for row in reader
    if normalize(row["customer"]) == normalize(customer)
    and normalize(row["site"]) == normalize(site)
    and normalize(row["program"]) == normalize(program)
    and normalize(row["data_type"]) == normalize(data_type)
]

if len(matches) == 1:
    row = matches[0]
    print(
        {
            "url": row["url"],
            "index_alias": row["index_alias"],
            "index_id": row["index_id"],
        }
    )
elif len(matches) > 1:
    raise ValueError(f"Ambiguous CSV match: {len(matches)} rows")
else:
    raise ValueError("No CSV match found")
```

Adjust the column names in the snippet if the CSV headings differ from `customer`, `site`, `program`, `data_type`, `url`, `index_alias`, and `index_id`.

## Cluster Lookup

Use this Python pattern. When needed, run it with `bash scripts/run_bg_vault_elastic_python.sh`:

```python
from bg_vault_elastic.client import VaultElasticClient

client = VaultElasticClient()
dev_creds = client.get_es_credentials(f"elastic-{customer_slug}-cluster")
```

Normalize the customer name to the lowercase slug expected by Vault. Example:

- `Washington` customer -> `elastic-washington-cluster`

## Connection Pattern

Prefer the Elasticsearch Python `8.x` client when talking to Elasticsearch `8.x` clusters. If the installed client is `9.x`, it can fail with a compatibility-header error. In that case either:

- use a version-compatible `8.x` client, or
- use `requests` directly until the client version is corrected

Preferred client shape:

```python
from elasticsearch import Elasticsearch

es_client = Elasticsearch(
    hosts=dev_creds["url"],
    basic_auth=(dev_creds["username"], dev_creds["password"]),
    request_timeout=30,
)
```

Fallback with `requests`:

```python
import requests

resp = requests.get(
    dev_creds["url"].rstrip("/") + "/",
    auth=(dev_creds["username"], dev_creds["password"]),
    headers={"Accept": "application/json"},
    timeout=30,
)
resp.raise_for_status()
```

## Name Normalization

Customer names and site names in conversation are not always the literal Elasticsearch alias parts.

Example mapping given a customer and site:

- customer `Washington` is the customer name
- site `Pittston` is the site name
- the Washington cluster secret is `elastic-washington-cluster`
- the Pittston site maps to `pit` in the log-records alias family

For this known case, prefer:

- exact alias family: `pit-washington-log.records-alias`
- wildcard scan: `*pit*washington*log.records*`

Do not assume the site token in Elasticsearch will be the full site name. If the full site name returns no matches, try a shortened token or common site code.

For customers with multiple sites, there will be multiple named Elasticsearch spaces, one for each site. For customers with one site the space is often named "Default" rather than the site name.

## Alias Discovery

When the exact alias is unknown, search in this order:

1. Exact alias guess: `{site_token}-{customer_token}-log.records-alias`
2. Reverse-order guess: `{customer_token}-{site_token}-log.records-alias`
3. Wildcards:
   - `*{site_token}*{customer_token}*log.records*`
   - `*{customer_token}*{site_token}*log.records*`
4. If the full site token fails, retry with a known short code

Use `_cat/indices` first:

```python
pattern = f"*{site_token}*{customer_token}*log.records*"
url = dev_creds["url"].rstrip("/") + f"/_cat/indices/{pattern}?format=json&h=index"
```

If needed, inspect aliases:

```python
url = dev_creds["url"].rstrip("/") + "/_cat/aliases?format=json&h=alias,index"
```

## Washington and Pittston Example

Use this as the default pattern when the user says the customer is Washington and the site is Pittston. When needed, run it with `bash scripts/run_bg_vault_elastic_python.sh`:

```python
from bg_vault_elastic.client import VaultElasticClient
import requests

customer_slug = "washington"
site_token = "pit"

client = VaultElasticClient()
dev_creds = client.get_es_credentials(f"elastic-{customer_slug}-cluster")

base = dev_creds["url"].rstrip("/")
auth = (dev_creds["username"], dev_creds["password"])

alias = f"{site_token}-{customer_slug}-log.records-alias"
r = requests.get(
    f"{base}/_cat/indices/{alias}*?format=json&h=index",
    auth=auth,
    headers={"Accept": "application/json"},
    timeout=30,
)
print(r.json())
```

## Response Rules

When reporting results back to the user:

- state the resolved Vault cluster name
- state the resolved Elasticsearch `.es.` endpoint
- state the exact alias or index actually found
- call out naming mismatches explicitly if the user used a business name that does not match the Elasticsearch token

For the Washington and Pittston case, explicitly note that:

- `Washington` refers to the customer and determines the cluster secret
- `Pittston` refers to the site
- the log-records alias token is `pit`, not `pittston`

## Data Structure

The data in the Elasticsearch indices is often structured in this way:

1. The "log.records" indices contain structured and unstructured logs.
2. The "grasps" indices contain data about each grasp.
3. The "picks" indices contain data about the robot picking an object. A pick may reference multiple grasps.
4. The "transfer" indices contain data about the robot transferring one or more items. A transfer may reference multiple picks.

Time-field reminder for common Berkshire Grey datasets:

- pick stats are typically queried on `date_created`, not `@timestamp`
- transfer stats are typically queried on `date_created`, not `@timestamp`
- metric events are typically queried on `@timestamp`
- wms data is typically queried on `@timestamp`
5. The "metrics" indices contain data used to track the system performance.
6. The "wms" indices contain data sent between the BG system and the customer's warehouse management system (wms).
