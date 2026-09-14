"""Compact research reports on disk; expand the existing API representation on read."""
from __future__ import annotations

import json
from typing import Any

FORMAT = "concept-report-v1"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def encode_result(result: dict) -> str:
    plain = _json(result)
    if not isinstance(result.get("concepts"), list):
        return plain
    pool, indexes = [], {}

    def visit(value: Any, concept_index: int | None = None) -> Any:
        if isinstance(value, list):
            return [visit(item, concept_index) for item in value]
        if not isinstance(value, dict):
            return value
        output = {}
        for key, item in value.items():
            if key == "concepts" and value is result:
                output[key] = [visit(concept, index) for index, concept in enumerate(item)]
            elif key == "sources" and isinstance(item, list):
                refs = []
                for source in item:
                    if not isinstance(source, dict) or not isinstance(source.get("id"), str):
                        # Never confuse an unknown legacy source with a compressed reference.
                        raise ValueError("unsupported legacy sources")
                    stored = {k: v for k, v in source.items() if k != "id"}
                    snapshot = None
                    if source.get("kind") == "data" and concept_index is not None:
                        concept = result["concepts"][concept_index]
                        for field in ("technicalData", "fundamentalData"):
                            if field in concept and source.get("excerpt") == json.dumps(concept[field], ensure_ascii=False):
                                stored.pop("excerpt")
                                snapshot = [concept_index, field]
                                break
                    entry = {"source": stored, "snapshot": snapshot}
                    identity = json.dumps(entry, ensure_ascii=False, sort_keys=True)
                    if identity not in indexes:
                        indexes[identity] = len(pool)
                        pool.append(entry)
                    refs.append([indexes[identity], source["id"]])
                output[key] = refs
            else:
                output[key] = visit(item, concept_index)
        return output

    try:
        payload = visit(result)
    except ValueError:
        return plain
    packed = _json({"storageFormat": FORMAT, "payload": payload, "sourcePool": pool})
    return packed if len(packed.encode()) < len(plain.encode()) else plain


def decode_result(raw: str) -> dict:
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get("storageFormat") != FORMAT:
        return result
    payload, pool = result["payload"], result["sourcePool"]

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        output = {}
        for key, item in value.items():
            if key == "sources" and isinstance(item, list):
                sources = []
                for ref in item:
                    if not isinstance(ref, list) or len(ref) != 2 or not isinstance(ref[0], int):
                        sources.append(ref)
                        continue
                    entry = pool[ref[0]]
                    source = {**entry["source"], "id": ref[1]}
                    if entry["snapshot"] is not None:
                        index, field = entry["snapshot"]
                        source["excerpt"] = json.dumps(payload["concepts"][index][field], ensure_ascii=False)
                    sources.append(source)
                output[key] = sources
            else:
                output[key] = visit(item)
        return output

    return visit(payload)
