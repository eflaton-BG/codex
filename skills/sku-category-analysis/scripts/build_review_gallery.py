#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import html
import json
import os
from pathlib import Path
import random
import re
from urllib.parse import quote


DEFAULT_BALANCED_PER_CATEGORY = 25
DEFAULT_OVERALL_RANDOM_COUNT = 150
DEFAULT_RANDOM_SEED = 202608


def relative_url(path: Path, start: Path) -> str:
    return quote(os.path.relpath(path, start=start.parent).replace("\\", "/"))


def contained_path(path: Path, job_dir: Path, description: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(job_dir):
        raise ValueError(
            f"{description} must remain inside the job directory {job_dir}: "
            f"{resolved}"
        )
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--images-dir", type=Path)
    parser.add_argument("--labels-dir", type=Path)
    parser.add_argument("--graph", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--title",
        default="Frontier Annotation Review",
    )
    parser.add_argument("--dataset-id")
    parser.add_argument(
        "--balanced-per-category",
        type=int,
        default=DEFAULT_BALANCED_PER_CATEGORY,
    )
    parser.add_argument(
        "--overall-random-count",
        type=int,
        default=DEFAULT_OVERALL_RANDOM_COUNT,
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_RANDOM_SEED)
    args = parser.parse_args()

    job_dir = args.job_dir.expanduser().resolve()
    if not job_dir.is_dir():
        raise FileNotFoundError(job_dir)
    if args.balanced_per_category < 0:
        raise ValueError("--balanced-per-category cannot be negative")
    if args.overall_random_count < 0:
        raise ValueError("--overall-random-count cannot be negative")

    images_dir = contained_path(
        args.images_dir or job_dir / "images",
        job_dir,
        "Images directory",
    )
    labels_dir = contained_path(
        args.labels_dir or job_dir / "labels",
        job_dir,
        "Labels directory",
    )
    output_dir = contained_path(
        args.output_dir or job_dir / "annotation-review",
        job_dir,
        "Review output directory",
    )
    graph_path = (
        contained_path(args.graph, job_dir, "Graph")
        if args.graph
        else None
    )

    if not images_dir.is_dir():
        raise FileNotFoundError(images_dir)
    if not labels_dir.is_dir():
        raise FileNotFoundError(labels_dir)
    if graph_path is not None and not graph_path.is_file():
        raise FileNotFoundError(graph_path)

    image_paths = {
        path.stem: path
        for path in images_dir.rglob("*.png")
        if path.is_file()
    }
    records: list[dict[str, str]] = []
    by_label: dict[str, list[dict[str, str]]] = defaultdict(list)
    for label_path in sorted(labels_dir.glob("*.txt")):
        stem = label_path.stem
        image_path = image_paths.get(stem)
        crop_path = labels_dir / f"{stem}_crop.png"
        if image_path is None or not crop_path.is_file():
            raise FileNotFoundError(f"Missing image or crop for {stem}")
        record = {
            "stem": stem,
            "label": label_path.read_text(encoding="utf-8").strip(),
            "original": relative_url(image_path, output_dir / "index.html"),
            "crop": relative_url(crop_path, output_dir / "index.html"),
            "date": image_path.parent.name,
        }
        records.append(record)
        by_label[record["label"]].append(record)

    rng = random.Random(args.seed)
    selected: dict[str, dict[str, str]] = {}
    sources: dict[str, list[str]] = defaultdict(list)
    for label in sorted(by_label):
        category_records = list(by_label[label])
        rng.shuffle(category_records)
        for record in category_records[: args.balanced_per_category]:
            selected[record["stem"]] = record
            sources[record["stem"]].append("balanced")

    remaining = [record for record in records if record["stem"] not in selected]
    rng.shuffle(remaining)
    for record in remaining[: args.overall_random_count]:
        selected[record["stem"]] = record
        sources[record["stem"]].append("overall")

    sample = []
    for record in selected.values():
        item = dict(record)
        item["sample_source"] = "+".join(sources[record["stem"]])
        sample.append(item)
    rng.shuffle(sample)

    counts = Counter(record["label"] for record in records)
    categories = [
        {"label": label, "count": count}
        for label, count in counts.most_common()
    ]

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "sample-manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "stem",
                "date",
                "predicted_label",
                "sample_source",
                "original_relative_path",
                "crop_relative_path",
            ],
        )
        writer.writeheader()
        for record in sample:
            writer.writerow(
                {
                    "stem": record["stem"],
                    "date": record["date"],
                    "predicted_label": record["label"],
                    "sample_source": record["sample_source"],
                    "original_relative_path": record["original"],
                    "crop_relative_path": record["crop"],
                }
            )

    data_json = json.dumps(sample, ensure_ascii=False).replace("</", "<\\/")
    categories_json = json.dumps(categories, ensure_ascii=False).replace(
        "</", "<\\/"
    )
    graph_link = ""
    if graph_path is not None:
        graph_url = relative_url(graph_path, output_dir / "index.html")
        graph_link = (
            f'<a href="{html.escape(graph_url)}">'
            "Open the category-distribution graph</a>."
        )
    safe_title = html.escape(args.title)
    dataset_id = args.dataset_id or job_dir.name
    safe_dataset_id = re.sub(
        r"[^a-zA-Z0-9_.-]+", "-", dataset_id
    ).strip("-")
    storage_key = f"{safe_dataset_id}-review-v1"
    export_filename = f"{safe_dataset_id}-annotation-review.csv"
    index_path = output_dir / "index.html"
    index_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>
:root {{
  color-scheme: light;
  --bg: #f4f6f8;
  --card: #ffffff;
  --border: #d8dee6;
  --text: #17202a;
  --muted: #5f6b78;
  --blue: #2563eb;
  --green: #16803c;
  --red: #c92a2a;
  --amber: #b16a00;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
header {{
  position: sticky;
  top: 0;
  z-index: 20;
  background: rgba(255,255,255,.97);
  border-bottom: 1px solid var(--border);
  padding: 14px 22px;
  box-shadow: 0 2px 10px rgba(0,0,0,.05);
}}
h1 {{ margin: 0 0 5px; font-size: 24px; }}
.subtitle {{ color: var(--muted); margin-bottom: 12px; }}
.controls {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}}
select, input, button, textarea {{
  font: inherit;
  border: 1px solid #b7c0cb;
  border-radius: 7px;
  background: white;
  padding: 8px 10px;
}}
button {{ cursor: pointer; }}
button.primary {{ background: var(--blue); color: white; border-color: var(--blue); }}
.progress {{ margin-left: auto; font-weight: 700; }}
.help {{ margin-top: 8px; color: var(--muted); font-size: 13px; }}
main {{ max-width: 1600px; margin: 0 auto; padding: 20px; }}
.summary {{
  background: white;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 18px;
  margin-bottom: 16px;
}}
.summary a {{ color: var(--blue); }}
.cards {{ display: grid; gap: 18px; }}
.card {{
  background: var(--card);
  border: 3px solid transparent;
  border-radius: 12px;
  box-shadow: 0 2px 10px rgba(0,0,0,.07);
  overflow: hidden;
  scroll-margin-top: 180px;
}}
.card.active {{ border-color: var(--blue); }}
.card[data-status="correct"] {{ box-shadow: inset 7px 0 var(--green), 0 2px 10px rgba(0,0,0,.07); }}
.card[data-status="incorrect"] {{ box-shadow: inset 7px 0 var(--red), 0 2px 10px rgba(0,0,0,.07); }}
.card[data-status="uncertain"] {{ box-shadow: inset 7px 0 var(--amber), 0 2px 10px rgba(0,0,0,.07); }}
.card-head {{
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}}
.predicted {{ font-weight: 800; font-size: 18px; }}
.meta {{ color: var(--muted); font-family: ui-monospace, monospace; }}
.badge {{
  color: #334155;
  background: #e8eef7;
  padding: 3px 8px;
  border-radius: 999px;
  font-size: 12px;
}}
.image-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: var(--border);
}}
.image-panel {{ background: #111; text-align: center; }}
.image-panel h3 {{
  margin: 0;
  padding: 7px;
  color: white;
  background: #27313d;
  font-size: 14px;
  font-weight: 600;
}}
.image-panel img {{
  display: block;
  width: 100%;
  height: 440px;
  object-fit: contain;
  background: #111;
}}
.review {{
  padding: 12px 16px 16px;
  display: grid;
  grid-template-columns: auto auto auto minmax(240px, 1fr) minmax(260px, 1fr);
  gap: 9px;
  align-items: center;
}}
.correct {{ color: var(--green); }}
.incorrect {{ color: var(--red); }}
.uncertain {{ color: var(--amber); }}
.empty {{
  background: white;
  border: 1px solid var(--border);
  padding: 30px;
  border-radius: 10px;
  text-align: center;
}}
@media (max-width: 950px) {{
  .image-grid {{ grid-template-columns: 1fr; }}
  .review {{ grid-template-columns: 1fr 1fr; }}
  .progress {{ margin-left: 0; }}
}}
</style>
</head>
<body>
<header>
  <h1>{safe_title}</h1>
  <div class="subtitle">Stratified sample of {len(sample):,} from {len(records):,} unique SKUs</div>
  <div class="controls">
    <select id="category"><option value="">All categories</option></select>
    <select id="status">
      <option value="">All review statuses</option>
      <option value="unreviewed">Unreviewed</option>
      <option value="correct">Correct</option>
      <option value="incorrect">Incorrect</option>
      <option value="uncertain">Uncertain</option>
    </select>
    <select id="source">
      <option value="">All sample sources</option>
      <option value="balanced">Balanced by category</option>
      <option value="overall">Overall random</option>
    </select>
    <input id="search" type="search" placeholder="Search SKU or label">
    <button id="next-unreviewed">Next unreviewed</button>
    <button id="export" class="primary">Export review CSV</button>
    <span id="progress" class="progress"></span>
  </div>
  <div class="help">Click a card to make it active. Keyboard: C correct, I incorrect, U uncertain, J/→ next, K/← previous.</div>
</header>
<main>
  <div class="summary">
    Sample design: up to {args.balanced_per_category} images from every observed category plus
    {args.overall_random_count} additional globally random images (seed {args.seed}).
    Reviews autosave in this browser.
    {graph_link}
  </div>
  <div id="cards" class="cards"></div>
</main>
<script>
const records = {data_json};
const categories = {categories_json};
const labels = categories.map(row => row.label);
const storageKey = {json.dumps(storage_key)};
const exportFilename = {json.dumps(export_filename)};
let reviews = JSON.parse(localStorage.getItem(storageKey) || "{{}}");
let activeStem = null;

const cardsEl = document.getElementById("cards");
const categoryEl = document.getElementById("category");
const statusEl = document.getElementById("status");
const sourceEl = document.getElementById("source");
const searchEl = document.getElementById("search");
const progressEl = document.getElementById("progress");

for (const row of categories) {{
  const option = document.createElement("option");
  option.value = row.label;
  option.textContent = `${{row.label}} (${{row.count.toLocaleString()}})`;
  categoryEl.appendChild(option);
}}

function stateFor(stem) {{
  return reviews[stem] || {{status: "", corrected_label: "", notes: ""}};
}}

function saveReview(stem, patch) {{
  reviews[stem] = Object.assign(stateFor(stem), patch);
  localStorage.setItem(storageKey, JSON.stringify(reviews));
  render();
}}

function filteredRecords() {{
  const category = categoryEl.value;
  const status = statusEl.value;
  const source = sourceEl.value;
  const search = searchEl.value.trim().toLowerCase();
  return records.filter(record => {{
    const review = stateFor(record.stem);
    if (category && record.label !== category) return false;
    if (status === "unreviewed" && review.status) return false;
    if (status && status !== "unreviewed" && review.status !== status) return false;
    if (source && !record.sample_source.includes(source)) return false;
    if (search && !`${{record.stem}} ${{record.label}} ${{record.date}}`.toLowerCase().includes(search)) return false;
    return true;
  }});
}}

function esc(value) {{
  return value.replace(/[&<>"']/g, character => ({{
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }})[character]);
}}

function labelOptions(selected) {{
  return `<option value="">Corrected label (if needed)</option>` +
    labels.map(label => `<option value="${{esc(label)}}" ${{label === selected ? "selected" : ""}}>${{esc(label)}}</option>`).join("");
}}

function setActive(stem) {{
  activeStem = stem;
  document.querySelectorAll(".card").forEach(card => {{
    card.classList.toggle("active", card.dataset.stem === stem);
  }});
}}

function render() {{
  const visible = filteredRecords();
  const reviewedCount = records.filter(record => stateFor(record.stem).status).length;
  progressEl.textContent = `${{reviewedCount.toLocaleString()}} / ${{records.length.toLocaleString()}} reviewed`;
  if (!visible.length) {{
    cardsEl.innerHTML = `<div class="empty">No images match the current filters.</div>`;
    return;
  }}
  cardsEl.innerHTML = visible.map(record => {{
    const review = stateFor(record.stem);
    return `
      <article class="card ${{record.stem === activeStem ? "active" : ""}}" data-stem="${{record.stem}}" data-status="${{review.status}}">
        <div class="card-head">
          <span class="predicted">${{esc(record.label)}}</span>
          <span class="meta">${{record.stem}} · ${{record.date}}</span>
          <span class="badge">${{record.sample_source}}</span>
        </div>
        <div class="image-grid">
          <div class="image-panel">
            <h3>Original image</h3>
            <img loading="lazy" src="${{record.original}}" alt="Original ${{record.stem}}">
          </div>
          <div class="image-panel">
            <h3>Annotated crop</h3>
            <img loading="lazy" src="${{record.crop}}" alt="Annotated crop ${{record.stem}}">
          </div>
        </div>
        <div class="review">
          <button class="mark correct" data-status="correct">✓ Correct</button>
          <button class="mark incorrect" data-status="incorrect">✕ Incorrect</button>
          <button class="mark uncertain" data-status="uncertain">? Uncertain</button>
          <select class="corrected">${{labelOptions(review.corrected_label)}}</select>
          <input class="notes" value="${{esc(review.notes)}}" placeholder="Review notes">
        </div>
      </article>`;
  }}).join("");

  document.querySelectorAll(".card").forEach(card => {{
    card.addEventListener("click", () => setActive(card.dataset.stem));
    card.querySelectorAll(".mark").forEach(button => {{
      button.addEventListener("click", event => {{
        event.stopPropagation();
        saveReview(card.dataset.stem, {{status: button.dataset.status}});
      }});
    }});
    card.querySelector(".corrected").addEventListener("change", event => {{
      saveReview(card.dataset.stem, {{corrected_label: event.target.value}});
    }});
    card.querySelector(".notes").addEventListener("change", event => {{
      saveReview(card.dataset.stem, {{notes: event.target.value}});
    }});
  }});
}}

function moveActive(delta) {{
  const visible = filteredRecords();
  if (!visible.length) return;
  let index = visible.findIndex(record => record.stem === activeStem);
  index = index < 0 ? 0 : Math.max(0, Math.min(visible.length - 1, index + delta));
  activeStem = visible[index].stem;
  render();
  document.querySelector(`[data-stem="${{activeStem}}"]`)?.scrollIntoView({{behavior: "smooth", block: "start"}});
}}

function markActive(status) {{
  if (!activeStem) {{
    activeStem = filteredRecords()[0]?.stem;
  }}
  if (activeStem) saveReview(activeStem, {{status}});
}}

async function exportCsv() {{
  const columns = ["stem", "date", "predicted_label", "sample_source", "review_status", "corrected_label", "notes"];
  const quoteCsv = value => `"${{String(value ?? "").replaceAll('"', '""')}}"`;
  const lines = [columns.join(",")];
  for (const record of records) {{
    const review = stateFor(record.stem);
    lines.push([
      record.stem, record.date, record.label, record.sample_source,
      review.status, review.corrected_label, review.notes
    ].map(quoteCsv).join(","));
  }}
  const text = lines.join("\\n") + "\\n";
  if (window.showSaveFilePicker) {{
    try {{
      const handle = await window.showSaveFilePicker({{
        suggestedName: exportFilename,
        types: [{{description: "CSV", accept: {{"text/csv": [".csv"]}}}}]
      }});
      const writable = await handle.createWritable();
      await writable.write(text);
      await writable.close();
      return;
    }} catch (error) {{
      if (error.name === "AbortError") return;
    }}
  }}
  const blob = new Blob([text], {{type: "text/csv;charset=utf-8"}});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = exportFilename;
  link.click();
  URL.revokeObjectURL(url);
}}

for (const control of [categoryEl, statusEl, sourceEl]) {{
  control.addEventListener("change", render);
}}
searchEl.addEventListener("input", render);
document.getElementById("export").addEventListener("click", exportCsv);
document.getElementById("next-unreviewed").addEventListener("click", () => {{
  const record = records.find(item => !stateFor(item.stem).status);
  if (!record) return;
  categoryEl.value = "";
  statusEl.value = "";
  sourceEl.value = "";
  searchEl.value = "";
  activeStem = record.stem;
  render();
  document.querySelector(`[data-stem="${{activeStem}}"]`)?.scrollIntoView({{behavior: "smooth", block: "start"}});
}});

document.addEventListener("keydown", event => {{
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  if (event.key === "c" || event.key === "C") markActive("correct");
  else if (event.key === "i" || event.key === "I") markActive("incorrect");
  else if (event.key === "u" || event.key === "U") markActive("uncertain");
  else if (event.key === "j" || event.key === "ArrowRight") moveActive(1);
  else if (event.key === "k" || event.key === "ArrowLeft") moveActive(-1);
}});

render();
</script>
</body>
</html>
""",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(index_path),
                "manifest": str(manifest_path),
                "dataset_records": len(records),
                "sample_records": len(sample),
                "categories": len(categories),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
