#!/usr/bin/env python3
"""Build a compact, decision-complete Codex subagent prompt."""

from __future__ import annotations

import argparse
import json


MODEL_IDS = {
    "luna": "gpt-5.6-luna",
    "terra": "gpt-5.6-terra",
    "sol": "gpt-5.6-sol",
}

MODEL_ROLES = {
    "luna": "bounded execution worker",
    "terra": "complex execution worker",
    "sol": "senior judgment and integration worker",
}


def bullet_section(title: str, values: list[str]) -> list[str]:
    if not values:
        return []
    return [title, *(f"- {value}" for value in values)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit spawn_agent arguments and a compact worker message as JSON."
    )
    parser.add_argument("--model", choices=MODEL_IDS, default="luna")
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default="medium",
    )
    parser.add_argument("--task", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--write-scope", action="append", default=[])
    parser.add_argument("--constraint", action="append", default=[])
    parser.add_argument("--verification", action="append", required=True)
    parser.add_argument("--deliverable", action="append", default=[])
    parser.add_argument("--coordination-log")
    parser.add_argument("--release-token")
    parser.add_argument("--fork-context", action="store_true")
    args = parser.parse_args()

    if args.release_token and not args.coordination_log:
        parser.error("--release-token requires --coordination-log")
    return args


def main() -> None:
    args = parse_args()
    writes = args.write_scope or ["No writes authorized."]
    deliverables = args.deliverable or [
        "Return a concise result with changed items, verification evidence, and blockers."
    ]

    lines = [
        f"You are the {MODEL_ROLES[args.model]}.",
        "",
        "TASK",
        args.task.strip(),
        "",
        "WORKING DIRECTORY",
        args.workdir,
        "",
        *bullet_section("AUTHORIZED WRITES", writes),
        "",
        *bullet_section("CONSTRAINTS", args.constraint),
        "",
        *bullet_section("VERIFY", args.verification),
        "",
        *bullet_section("DELIVER", deliverables),
    ]

    if args.coordination_log:
        coordination = [
            f"Use the shared coordination log at {args.coordination_log}.",
            "Acknowledge coordinator gates before acting and report progress there.",
            "Do not contact the user directly; report to the coordinator.",
        ]
        if args.release_token:
            coordination.insert(1, f"Required release token: {args.release_token}")
        lines.extend(["", *bullet_section("COORDINATION", coordination)])

    lines.extend(
        [
            "",
            "Stop and report blockers or concurrent changes instead of broadening scope.",
        ]
    )

    message = "\n".join(line for line in lines if line is not None).strip()
    payload = {
        "model": MODEL_IDS[args.model],
        "reasoning_effort": args.reasoning_effort,
        "fork_context": args.fork_context,
        "message": message,
    }
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
