#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run MCP smoke tests against Bourdon L6 server.")
    parser.add_argument("--library-path")
    parser.add_argument("--entity-name", default="Bourdon MCP")
    parser.add_argument("--query-topic", default="Bourdon MCP")
    parser.add_argument(
        "--recognition-prompt",
        default="",
        help="Prompt used to exercise prepare_recognition_context.",
    )
    parser.add_argument(
        "--expected-bourdon-summary",
        default="",
    )
    parser.add_argument("--assertions", action="store_true")
    parser.add_argument("--json-report", default="")
    parser.add_argument(
        "--server-python",
        default=sys.executable,
        help="Python executable used to launch the Bourdon L6 server.",
    )
    parser.add_argument(
        "--federation-write-roundtrip",
        action="store_true",
        help=(
            "Exercise commit_to_federation then verify read-back via find_entity "
            "(v0.6.0 write path). Prefer --library-path on a disposable directory "
            "so probes do not accumulate under ~/agent-library."
        ),
    )
    parser.add_argument(
        "--skip-seeded-library-assertions",
        action="store_true",
        help=(
            "Skip assertions that require a populated federation (e.g. find_entity "
            "matches for entity-name, entities in get_cross_agent_summary, non-empty "
            "prepare_recognition_context). Use with a disposable --library-path so an "
            "empty store can still prove tool wiring or write round-trips."
        ),
    )
    parser.add_argument(
        "--isolate-federation-write-smoke",
        action="store_true",
        help=(
            "Shorthand: enable --federation-write-roundtrip plus "
            "--skip-seeded-library-assertions — intended for throwaway --library-path dirs."
        ),
    )
    args = parser.parse_args()
    if args.isolate_federation_write_smoke:
        args.federation_write_roundtrip = True
        args.skip_seeded_library_assertions = True
    if args.federation_write_roundtrip and not args.library_path:
        parser.error(
            "--federation-write-roundtrip / --isolate-federation-write-smoke "
            "require --library-path to be set to a disposable directory; refusing "
            "to write probe agents into the default ~/agent-library/."
        )
    if args.library_path is None:
        args.library_path = str(Path.home() / "agent-library")
    if not args.recognition_prompt:
        args.recognition_prompt = f"Tell me about {args.entity_name}."
    return args


def _first_json_payload(call_result: Any) -> dict[str, Any]:
    for item in call_result.content:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return {}


async def _run(args: argparse.Namespace) -> int:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    report: dict[str, Any] = {
        "status": "pass",
        "tools": [],
        "checks": {},
        "payloads": {},
    }
    server = StdioServerParameters(
        command=str(args.server_python),
        args=[
            "-m",
            "core.l6_server",
            "--library",
            str(args.library_path),
            "--transport",
            "stdio",
        ],
    )

    async with (
        stdio_client(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        tool_names = [tool.name for tool in tools.tools]
        report["tools"] = sorted(tool_names)

        find_result = await session.call_tool(
            "find_entity",
            {"name": args.entity_name, "access_level": "team"},
        )
        summary_result = await session.call_tool(
            "get_cross_agent_summary",
            {"project": args.entity_name, "access_level": "team"},
        )
        query_result = await session.call_tool(
            "query_agent_memory",
            {"agent": "cursor", "topic": args.query_topic, "access_level": "team"},
        )
        recent_result = await session.call_tool(
            "list_recent_work",
            {"access_level": "team"},
        )
        recognition_result = await session.call_tool(
            "prepare_recognition_context",
            {"prompt": args.recognition_prompt, "access_level": "team"},
        )
        deeper_result = await session.call_tool(
            "get_deeper_context",
            {"prompt": args.recognition_prompt, "access_level": "team"},
        )

        roundtrip_agent_id = ""
        roundtrip_entity_name = ""
        commit_result: Any = None
        verify_roundtrip_result: Any = None
        if args.federation_write_roundtrip:
            token = uuid.uuid4().hex[:12]
            roundtrip_agent_id = f"mcp_smoke_{token}"
            roundtrip_entity_name = f"McpSmokeEntity_{token}"
            commit_result = await session.call_tool(
                "commit_to_federation",
                {
                    "agent_id": roundtrip_agent_id,
                    "agent_type": "other",
                    "entities": [
                        {
                            "name": roundtrip_entity_name,
                            "summary": "Bourdon MCP write/read-back probe via mcp_smoke_test.",
                        },
                    ],
                    "mode": "merge",
                },
            )
            verify_roundtrip_result = await session.call_tool(
                "find_entity",
                {"name": roundtrip_entity_name, "access_level": "team"},
            )

        print("TOOLS:", ", ".join(sorted(tool_names)))
        for label, result in [
            ("FIND_ENTITY_RAW", find_result),
            ("CROSS_AGENT_SUMMARY_RAW", summary_result),
            ("QUERY_AGENT_MEMORY_RAW", query_result),
            ("LIST_RECENT_WORK_RAW", recent_result),
            ("PREPARE_RECOGNITION_CONTEXT_RAW", recognition_result),
            ("GET_DEEPER_CONTEXT_RAW", deeper_result),
            *(
                [
                    ("COMMIT_TO_FEDERATION_RAW", commit_result),
                    ("FIND_ENTITY_VERIFY_ROUNDTRIP_RAW", verify_roundtrip_result),
                ]
                if args.federation_write_roundtrip
                else []
            ),
        ]:
            print(f"{label}:")
            for item in result.content:
                if hasattr(item, "text"):
                    print(item.text)

        find_payload = _first_json_payload(find_result)
        summary_payload = _first_json_payload(summary_result)
        query_payload = _first_json_payload(query_result)
        recent_payload = _first_json_payload(recent_result)
        recognition_payload = _first_json_payload(recognition_result)
        deeper_payload = _first_json_payload(deeper_result)
        commit_payload = _first_json_payload(commit_result) if commit_result else {}
        verify_roundtrip_payload = (
            _first_json_payload(verify_roundtrip_result) if verify_roundtrip_result else {}
        )
        report["payloads"] = {
            "find_entity": find_payload,
            "get_cross_agent_summary": summary_payload,
            "query_agent_memory": query_payload,
            "list_recent_work": recent_payload,
            "prepare_recognition_context": recognition_payload,
            "get_deeper_context": deeper_payload,
        }
        if args.federation_write_roundtrip:
            report["payloads"]["commit_to_federation"] = commit_payload
            report["payloads"]["find_entity_verification_roundtrip"] = verify_roundtrip_payload

        if args.assertions:
            required_tools = {
                "find_entity",
                "get_deeper_context",
                "get_cross_agent_summary",
                "list_recent_work",
                "prepare_recognition_context",
                "query_agent_memory",
                "commit_to_federation",
            }
            missing_tools = sorted(required_tools.difference(set(tool_names)))
            report["checks"]["required_tools"] = len(missing_tools) == 0
            if missing_tools:
                raise AssertionError(f"Missing MCP tools: {missing_tools}")

            if not args.skip_seeded_library_assertions:
                report["checks"]["find_entity_has_matches"] = bool(
                    find_payload.get("matches")
                )
                if not report["checks"]["find_entity_has_matches"]:
                    raise AssertionError(
                        f"find_entity returned no matches for {args.entity_name}."
                    )

                cursor_summary = find_payload["matches"][0].get("summaries", {}).get(
                    "cursor", ""
                )
                if args.expected_bourdon_summary:
                    report["checks"]["bourdon_summary_matches_expected"] = (
                        cursor_summary == args.expected_bourdon_summary
                    )
                    if not report["checks"]["bourdon_summary_matches_expected"]:
                        raise AssertionError(
                            "Unexpected summary. Expected "
                            f"{args.expected_bourdon_summary!r}, got {cursor_summary!r}"
                        )
                else:
                    report["checks"]["bourdon_summary_matches_expected"] = True

                report["checks"]["cross_agent_summary_has_entities"] = bool(
                    summary_payload.get("entities")
                )
                if not report["checks"]["cross_agent_summary_has_entities"]:
                    raise AssertionError("get_cross_agent_summary returned no entities.")

                report["checks"]["query_agent_memory_has_matches"] = bool(
                    query_payload.get("matches")
                )
                if not report["checks"]["query_agent_memory_has_matches"]:
                    raise AssertionError(
                        f"query_agent_memory returned no matches for {args.query_topic}."
                    )
            else:
                report["checks"]["seeded_library_skipped"] = True

            report["checks"]["list_recent_work_has_sessions_field"] = (
                "sessions" in recent_payload
            )
            if not report["checks"]["list_recent_work_has_sessions_field"]:
                raise AssertionError("list_recent_work response is missing sessions field.")

            if not args.skip_seeded_library_assertions:
                recognition_check = bool(recognition_payload.get("recognition"))
                report["checks"]["prepare_recognition_context_has_recognition"] = (
                    recognition_check
                )
                if not recognition_check:
                    raise AssertionError(
                        "prepare_recognition_context returned no recognition "
                        f"for {args.recognition_prompt!r}."
                    )

                prompt_context_check = bool(recognition_payload.get("prompt_context"))
                report["checks"]["prepare_recognition_context_has_prompt_context"] = (
                    prompt_context_check
                )
                if not prompt_context_check:
                    raise AssertionError(
                        "prepare_recognition_context response is missing prompt_context."
                    )
            else:
                has_pc = "prompt_context" in recognition_payload
                report["checks"]["prepare_recognition_context_has_prompt_context_key"] = (
                    has_pc
                )
                if not has_pc:
                    raise AssertionError(
                        "prepare_recognition_context response is missing prompt_context key."
                    )

            deeper_context_check = "context" in deeper_payload
            report["checks"]["get_deeper_context_has_context_field"] = (
                deeper_context_check
            )
            if not deeper_context_check:
                raise AssertionError("get_deeper_context response is missing context.")

            if args.federation_write_roundtrip:
                commit_err = commit_payload.get("error")
                report["checks"]["commit_to_federation_succeeded"] = commit_err is None
                if commit_err:
                    raise AssertionError(
                        f"commit_to_federation failed: {commit_err!r}; "
                        f"payload keys={sorted(commit_payload)}"
                    )
                roundtrip_matches = verify_roundtrip_payload.get("matches") or []
                report["checks"]["find_entity_verifies_commit_roundtrip"] = bool(
                    roundtrip_matches
                )
                if not roundtrip_matches:
                    raise AssertionError(
                        "find_entity found no matches after commit_to_federation "
                        f"for {roundtrip_entity_name!r} (agent {roundtrip_agent_id!r})."
                    )

    if args.json_report:
        report_path = Path(args.json_report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    parsed_args = _parse_args()
    try:
        raise SystemExit(asyncio.run(_run(parsed_args)))
    except Exception as exc:  # noqa: BLE001
        if parsed_args.json_report:
            report_path = Path(parsed_args.json_report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps({"status": "fail", "error": str(exc)}, indent=2),
                encoding="utf-8",
            )
        raise
