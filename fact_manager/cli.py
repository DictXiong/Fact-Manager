import argparse
import json
import sys

from .catalog import Catalog
from .core import WorkflowBusy, load_config
from .workflow import Ragflow, export_review, extract, import_review


def main():
    parser = argparse.ArgumentParser(
        description="Independent fact libraries, evidence, web review and scoped MCP"
    )
    parser.add_argument("--config", default="/etc/fact-manager/config.json")
    parser.add_argument(
        "--library", help="Fact library ID for library-specific operations"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("sync", "extract", "check-local", "sources", "serve", "libraries"):
        commands.add_parser(command)
    create = commands.add_parser("library-create")
    create.add_argument("name")
    bind = commands.add_parser("bind")
    bind.add_argument("dataset_id")
    migrate = commands.add_parser("migrate-legacy")
    migrate.add_argument("path")
    migrate.add_argument("--dataset", action="append", required=True)
    migrate.add_argument("--name", default="Harnets.AI")
    export = commands.add_parser("review-export")
    export.add_argument("path")
    review = commands.add_parser("review-import")
    review.add_argument("path")
    review.add_argument("--reviewer", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--reviewer", required=True)
    retire = commands.add_parser("retire")
    retire.add_argument("id")
    retire.add_argument("--reviewer", required=True)
    retire.add_argument("--reason", required=True)
    verified = commands.add_parser("verified")
    verified.add_argument("--entity", default="")
    verified.add_argument("--attribute", default="")
    verified.add_argument("--internal", action="store_true")
    check = commands.add_parser("check")
    check.add_argument(
        "--fact",
        action="append",
        help="Limit model checking to these pending fact IDs (repeatable, max40)",
    )
    packet = commands.add_parser("review-packet")
    packet.add_argument("--offset", type=int, default=0)
    packet.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        catalog = Catalog(config)
        if args.command == "serve":
            from .server import serve

            serve(catalog, config)
            return
        if args.command == "libraries":
            result = [
                {"id": l["id"], "name": l["name"], "dataset_ids": l["dataset_ids"]}
                for l in catalog.libraries()
            ]
        elif args.command == "library-create":
            result = catalog.create(args.name)
        elif args.command == "migrate-legacy":
            result = catalog.migrate_legacy(
                args.path, args.dataset, args.name, args.library or "harnets"
            )
        elif args.command == "sync":
            libraries = (
                [catalog.library(args.library)] if args.library else catalog.libraries()
            )
            from .checking import local_check

            result = {"libraries": []}
            for library in libraries:
                store = catalog.store(library["id"])
                try:
                    with store.workflow_lock(), Ragflow(config) as ragflow:
                        result["libraries"].append(
                            {
                                "id": library["id"],
                                "snapshotted_documents": ragflow.sync(
                                    store, catalog.excluded(library["id"])
                                ),
                                "local_check": local_check(store),
                            }
                        )
                except WorkflowBusy:
                    if args.library:
                        raise
                    result["libraries"].append({"id": library["id"], "skipped": "busy"})
        else:
            if not args.library:
                raise ValueError("Specify --library for this operation")
            store = catalog.store(args.library)
            if args.command == "bind":
                catalog.bind(args.library, args.dataset_id)
                result = {"bound_dataset": args.dataset_id}
            elif args.command == "extract":
                from .checking import local_check

                with store.workflow_lock():
                    result = {
                        "new_candidates": extract(
                            store, catalog.workflow_config(args.library)
                        ),
                        "local_check": local_check(store),
                    }
            elif args.command in ("check", "check-local", "review-packet"):
                from .checking import check_with_model, local_check, review_packet

                if args.command == "review-packet":
                    result = review_packet(store, offset=args.offset, limit=args.limit)
                else:
                    with store.workflow_lock():
                        result = (
                            check_with_model(
                                store,
                                catalog.workflow_config(args.library),
                                fact_ids=args.fact,
                            )
                            if args.command == "check"
                            else local_check(store)
                        )
            elif args.command == "sources":
                result = store.sources()
            elif args.command == "review-export":
                result = {"exported_candidates": export_review(store, args.path)}
            elif args.command == "review-import":
                result = {
                    "reviewed_candidates": import_review(
                        store, args.path, args.reviewer
                    )
                }
            elif args.command == "publish":
                result = {"published_facts": store.publish(args.reviewer)}
            elif args.command == "retire":
                store.retire(args.id, args.reviewer, args.reason)
                result = {"retired": args.id}
            else:
                result = store.verified(args.entity, args.attribute, not args.internal)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1
