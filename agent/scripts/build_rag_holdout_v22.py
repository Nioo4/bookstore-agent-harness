"""Create the frozen v2.2 holdout by correcting one reviewed policy label.

V2.1 incorrectly required abstention for automatic refunds even though the
curated policy explicitly says refunds are unsupported.  Retrieval of that
negative rule is correct behavior.  No query, threshold or other label changes.
Do not edit the generated split after its first evaluation.
"""
from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "evals" / "rag-v2.1" / "holdout.jsonl"
    target = root / "evals" / "rag-v2.2" / "holdout.jsonl"
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 56:
        raise RuntimeError("expected exactly 56 frozen cases")
    matches = [row for row in rows if row["case_id"] == "rag-holdout-v21-policy-026"]
    if len(matches) != 1:
        raise RuntimeError("expected the reviewed automatic-refund case")
    row = matches[0]
    row["case_id"] = "rag-holdout-v22-policy-026"
    row["expected_document_ids"] = ["policy-demo-scope"]
    row["expected_sections"] = ["退款退货与换货"]
    row["must_abstain"] = False
    for other in rows:
        other["case_id"] = other["case_id"].replace("rag-holdout-v21-", "rag-holdout-v22-")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in rows), encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
