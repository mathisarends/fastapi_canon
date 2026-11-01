import argparse
import json
from copy import deepcopy
from pathlib import Path

from fastapi_canon.integrations.sse.codegen.openapi import (
    ContractError,
    Document,
    validate_contract,
)


def rest_codegen_document(document: Document) -> Document:
    """Derive a private 3.1 codegen view of ordinary REST plus JSON SSE contracts.

    This example covers itemSchema/data/contentSchema, not arbitrary OAS 3.2.
    The SSE schema denotes decoded event data only in this private view.
    """
    validate_contract(document)
    result = deepcopy(document)
    result["openapi"] = "3.1.0"
    result.pop("jsonSchemaDialect", None)
    for path in result.get("paths", {}).values():
        for method, operation in path.items():
            if method not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
                "trace",
            }:
                continue
            for response in operation.get("responses", {}).values():
                for media_type, media in response.get("content", {}).items():
                    if "itemSchema" not in media:
                        continue
                    if media_type != "text/event-stream":
                        raise ContractError(
                            "This recipe only adapts JSON SSE itemSchema"
                        )
                    envelope = media.pop("itemSchema")
                    try:
                        data = envelope["properties"]["data"]
                        if data["contentMediaType"] != "application/json":
                            raise KeyError("contentMediaType")
                        media["schema"] = data["contentSchema"]
                    except (KeyError, TypeError) as exc:
                        raise ContractError(
                            "Expected inline JSON SSE envelope"
                        ) from exc
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Derive a private REST codegen view")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = (
        json.dumps(
            rest_codegen_document(json.loads(args.source.read_text("utf-8"))),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if args.check:
        if not args.output.is_file() or args.output.read_text("utf-8") != content:
            raise SystemExit("Private REST codegen view is out of date")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
