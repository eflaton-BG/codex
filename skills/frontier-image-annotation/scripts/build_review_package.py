#!/usr/bin/env python3
"""Build a category-balanced local review and SKU distribution chart; no API calls."""
import argparse
from collections import Counter
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont


def draw_chart(counts, month, destination, image_count):
    width, height = 2400, 1400
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)
    font_dir = Path("/usr/share/fonts/truetype/dejavu")
    normal = ImageFont.truetype(str(font_dir / "DejaVuSans.ttf"), 23)
    small = ImageFont.truetype(str(font_dir / "DejaVuSans.ttf"), 20)
    title = ImageFont.truetype(str(font_dir / "DejaVuSans-Bold.ttf"), 42)
    left, right, top, bottom = 900, 2210, 160, 1210
    total = sum(counts.values())
    ceiling = math.ceil(max(counts.values()) * 1.15 / 5000) * 5000
    palette = [
        "#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2",
        "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
        "#5b8ff9", "#61d9a8", "#65789b", "#f6bd16", "#7262fd",
    ]
    draw.text((width / 2, 65), f"Product Category Distribution — {month}",
              font=title, fill="#111111", anchor="mm")
    for tick in range(0, ceiling + 1, 5000):
        x = left + (right - left) * tick / ceiling
        draw.line((x, top, x, bottom), fill="#dddddd", width=2)
        draw.text((x, bottom + 32), f"{tick:,}", font=normal, fill="#333333", anchor="mm")
    ordered = counts.most_common()
    step = (bottom - top) / len(ordered)
    for i, (label, count) in enumerate(ordered):
        y = top + step * (i + 0.5)
        end = left + (right - left) * count / ceiling
        draw.text((left - 16, y), label, font=normal, fill="#222222", anchor="rm")
        draw.rectangle((left, y - 21, end, y + 21), fill=palette[i % len(palette)])
        percentage = 100 * count / total
        pct = "<0.1%" if 0 < percentage < 0.1 else f"{percentage:.1f}%"
        draw.text((end + 16, y), f"{pct} ({count:,})",
                  font=normal, fill="#555555", anchor="lm")
    draw.line((left, top, left, bottom, right, bottom), fill="#333333", width=2)
    draw.text(((left + right) / 2, bottom + 76), "Unique SKU ID Count",
              font=normal, fill="#333333", anchor="mm")
    draw.text((70, 1335), "Draft model predictions — human review pending",
              font=small, fill="#666666")
    draw.text((width - 70, 1335),
              f"Mapped SKUs: {total:,}  |  Distinct images: {image_count:,}",
              font=small, fill="#666666", anchor="ra")
    canvas.save(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--labels-dir", type=Path, required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument("--sample-size", type=int, default=300)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()
    job = args.job_dir.resolve()
    labels_dir = args.labels_dir.resolve()
    assert labels_dir.is_relative_to(job)
    output = job / f"annotation-review-{args.sample_size}"
    if output.exists():
        raise SystemExit(f"Refusing to overwrite existing review: {output}")
    labels = {p.stem: p.read_text().strip() for p in labels_dir.glob("*.txt")}
    image_counts = Counter(labels.values())
    assert 0 < args.sample_size <= len(labels)
    if args.sample_size < len(image_counts):
        raise ValueError("Sample size must cover every observed category")
    with (job / "manifest/one-image-per-sku.csv").open() as stream:
        mappings = list(csv.DictReader(stream))
    assert len({r["SkuId"] for r in mappings}) == len(mappings)
    assert {Path(r["s3_uri"]).stem for r in mappings} == set(labels)
    sku_counts = Counter(labels[Path(r["s3_uri"]).stem] for r in mappings)
    # Maximize the equal-category quota; rare categories contribute all examples.
    quota = 0
    while sum(min(n, quota + 1) for n in image_counts.values()) <= args.sample_size:
        quota += 1
        if sum(min(n, quota) for n in image_counts.values()) == len(labels):
            break
    balanced = sum(min(n, quota) for n in image_counts.values())
    top_up = args.sample_size - balanced
    graph = job / f"Product Category Distribution {args.month}.png"
    if graph.exists():
        raise SystemExit(f"Refusing to overwrite existing graph: {graph}")
    draw_chart(sku_counts, args.month, graph, len(labels))
    builder = Path(__file__).with_name("build_review_gallery.py")
    subprocess.run([
        sys.executable, str(builder), "--job-dir", str(job),
        "--labels-dir", str(labels_dir), "--output-dir", str(output),
        "--graph", str(graph), "--balanced-per-category", str(quota),
        "--overall-random-count", str(top_up), "--seed", str(args.seed),
        "--dataset-id", f"{job.name}-balanced-{args.sample_size}-{args.seed}",
        "--title", f"{args.month} — {args.sample_size}-Image Category-Balanced Review",
    ], check=True)
    with (output / "sample-manifest.csv").open() as stream:
        sample = list(csv.DictReader(stream))
    assert len(sample) == len({r["stem"] for r in sample}) == args.sample_size
    selected_counts = Counter(r["predicted_label"] for r in sample)
    assert set(selected_counts) == set(image_counts)
    for label, count in image_counts.items():
        assert selected_counts[label] >= min(quota, count)
    summary = {
        "created_at": datetime.now().astimezone().isoformat(),
        "seed": args.seed, "sample_size": len(sample),
        "balanced_quota": quota, "balanced_samples": balanced,
        "random_top_up": top_up, "sample_counts": selected_counts,
        "sample_dates": dict(sorted(Counter(r["date"] for r in sample).items())),
        "population_image_counts": image_counts,
        "population_sku_counts": sku_counts,
        "graph": str(graph),
        "note": "Category-balanced review is not a prevalence-weighted accuracy sample. "
                "Chart counts all mapped unique SKUs, not the review subset.",
    }
    (output / "sampling-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
