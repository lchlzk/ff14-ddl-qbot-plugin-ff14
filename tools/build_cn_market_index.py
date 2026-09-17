from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def build_index(
    item_csv: Path,
    marketable_json: Path,
    output: Path,
    source_commit: str,
) -> int:
    marketable_payload = json.loads(marketable_json.read_text(encoding="utf-8"))
    if not isinstance(marketable_payload, list):
        raise ValueError("Universalis marketable response must be a JSON list")
    marketable_ids = {
        int(value)
        for value in marketable_payload
        if isinstance(value, int) or (isinstance(value, str) and value.isascii() and value.isdigit())
    }

    records: list[tuple[int, str]] = []
    with item_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = csv.reader(handle)
        for _ in range(3):
            next(rows, None)
        for row in rows:
            if len(row) < 2 or not row[0].isascii() or not row[0].isdigit():
                continue
            item_id = int(row[0])
            if item_id not in marketable_ids:
                continue
            name = row[1].replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()
            if name:
                records.append((item_id, name))

    records.sort()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# FFXIV CN marketable item index\n")
        handle.write("# source=https://github.com/thewakingsands/ffxiv-datamining-cn/Item.csv\n")
        handle.write(f"# source_commit={source_commit}\n")
        handle.write("# marketable=https://universalis.app/api/v2/marketable\n")
        for item_id, name in records:
            handle.write(f"{item_id}\t{name}\n")
    temporary.replace(output)
    return len(records)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the compact CN market item index")
    parser.add_argument("item_csv", type=Path)
    parser.add_argument("marketable_json", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    count = build_index(
        args.item_csv,
        args.marketable_json,
        args.output,
        args.source_commit,
    )
    print(f"Wrote {count} marketable item names to {args.output}")


if __name__ == "__main__":
    main()
