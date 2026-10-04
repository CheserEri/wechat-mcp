"""阶段 4 真实联调：以 MCP stdio 协议驱动 Server，验证 P1 能力。

用法（项目根目录，需微信已登录且主窗口可见）::

    .venv\\Scripts\\python.exe scripts\\phase4_live_check.py

所有发送测试都发往「文件传输助手」（仅发给自己），不会打扰真实联系人。
输出每行形如 ``RESULT <场景> PASS|FAIL <详情>``，便于记录测试结果。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

SELF_TARGET = "文件传输助手"
ALL_TOOLS = {
    "get_wechat_status",
    "get_chat_list",
    "get_chat_history",
    "send_message",
    "search_contact",
    "get_chat_info",
    "send_file",
    "get_recent_messages",
    "set_monitored_chats",
}


def _payload(result) -> dict:
    if getattr(result, "structured_content", None):
        return result.structured_content
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


def report(scenario: str, passed: bool, detail: str) -> bool:
    print(f"RESULT {scenario} {'PASS' if passed else 'FAIL'} {detail}", flush=True)
    return passed


def server_params(env_overrides: dict[str, str] | None = None) -> StdioServerParameters:
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "wechat_mcp.server"],
        env=env,
    )


async def run_main_scenarios(send_dir: Path, audit_log: Path) -> list[bool]:
    results: list[bool] = []
    params = server_params(
        {
            "WECHAT_SEND_DIRS": str(send_dir),
            "WECHAT_AUDIT_LOG": str(audit_log),
            "WECHAT_MONITOR_MODE": "allow",
            "WECHAT_MONITOR_ALLOW": "",
            "WECHAT_MONITOR_BLOCK": "",
        }
    )
    test_file = send_dir / "phase4_测试文件.txt"
    test_file.write_text("phase4 live check 测试内容", encoding="utf-8")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            names = {tool.name for tool in (await session.list_tools()).tools}
            results.append(
                report("0-tool-discovery", names == ALL_TOOLS, f"tools={sorted(names)}")
            )

            status = _payload(await session.call_tool("get_wechat_status", {}))
            results.append(
                report(
                    "1-status",
                    bool(status.get("ok")) and status.get("state") == "connected",
                    f"state={status.get('state')} backend={status.get('backend')}",
                )
            )

            # 场景 2：联系人搜索（同名候选不猜测）
            search = _payload(
                await session.call_tool("search_contact", {"keyword": "文件传输助手"})
            )
            results.append(
                report(
                    "2-search-contact",
                    bool(search.get("ok")),
                    f"source={search.get('source')} total={search.get('total')} "
                    f"names={[c.get('name') for c in search.get('candidates') or []]}",
                )
            )

            # 场景 3：会话信息
            info = _payload(
                await session.call_tool("get_chat_info", {"chat_name": SELF_TARGET})
            )
            results.append(
                report(
                    "3-chat-info",
                    bool(info.get("ok")) and info.get("name") == SELF_TARGET,
                    f"is_group={info.get('is_group')} monitored={info.get('monitored')}",
                )
            )

            # 场景 4：增量读取游标推进
            first = _payload(
                await session.call_tool("get_recent_messages", {"after_seq": 0, "limit": 5})
            )
            cursor = int(first.get("next_seq") or 0)
            second = _payload(
                await session.call_tool(
                    "get_recent_messages", {"after_seq": cursor, "limit": 5}
                )
            )
            results.append(
                report(
                    "4-recent-messages",
                    bool(first.get("ok"))
                    and bool(second.get("ok"))
                    and int(second.get("next_seq") or 0) >= cursor,
                    f"next_seq={cursor} second_count={len(second.get('messages') or [])}",
                )
            )

            # 场景 5：关注列表（空 allow=全部；设置后仅保留指定会话）
            updated = _payload(
                await session.call_tool(
                    "set_monitored_chats", {"add": [SELF_TARGET], "mode": "allow"}
                )
            )
            listing = _payload(await session.call_tool("get_chat_list", {}))
            names_in_list = [c.get("name") for c in listing.get("chats") or []]
            filtered_ok = bool(updated.get("ok")) and all(
                name == SELF_TARGET for name in names_in_list
            )
            results.append(
                report(
                    "5-monitored-chats",
                    filtered_ok,
                    f"allow={updated.get('allow')} visible={names_in_list}",
                )
            )
            # 复位为关注全部
            await session.call_tool(
                "set_monitored_chats",
                {"remove": [SELF_TARGET], "mode": "allow"},
            )

            # 场景 6：发送文件 dry-run（不实际发送）
            dry = _payload(
                await session.call_tool(
                    "send_file",
                    {
                        "recipient": SELF_TARGET,
                        "file_path": str(test_file),
                        "dry_run": True,
                    },
                )
            )
            results.append(
                report(
                    "6-send-file-dry-run",
                    bool(dry.get("ok")) and dry.get("status") == "dry_run",
                    f"file={dry.get('file_name')} size={dry.get('file_size')}",
                )
            )

            # 场景 7：发送文件白名单外被拒绝
            outside = Path(tempfile.gettempdir()) / "phase4_outside.txt"
            outside.write_text("x", encoding="utf-8")
            try:
                rejected = _payload(
                    await session.call_tool(
                        "send_file",
                        {"recipient": SELF_TARGET, "file_path": str(outside)},
                    )
                )
            finally:
                outside.unlink(missing_ok=True)
            results.append(
                report(
                    "7-send-file-outside-rejected",
                    (not rejected.get("ok"))
                    and (rejected.get("error") or {}).get("code") == "path_not_allowed",
                    f"code={(rejected.get('error') or {}).get('code')}",
                )
            )

            # 场景 8：真实发送文件到文件传输助手（仅发给自己）
            sent = _payload(
                await session.call_tool(
                    "send_file",
                    {"recipient": SELF_TARGET, "file_path": str(test_file)},
                )
            )
            results.append(
                report(
                    "8-send-file",
                    bool(sent.get("ok")) and sent.get("status") == "sent",
                    f"status={sent.get('status')} resolved={sent.get('resolved_recipient')}",
                )
            )

    return results


async def run_wrong_whitelist_scenario() -> bool:
    """未配置可发送目录时，send_file 必须被拒绝。"""
    with tempfile.TemporaryDirectory() as tmp:
        params = server_params(
            {"WECHAT_SEND_DIRS": "", "WECHAT_AUDIT_ENABLED": "0"}
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                target = Path(tmp) / "a.txt"
                target.write_text("x", encoding="utf-8")
                result = _payload(
                    await session.call_tool(
                        "send_file",
                        {"recipient": SELF_TARGET, "file_path": str(target)},
                    )
                )
    code = (result.get("error") or {}).get("code")
    return report(
        "9-no-whitelist-rejected",
        (not result.get("ok")) and code == "path_not_allowed",
        f"code={code}",
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="wechat-mcp 阶段4 真实联调")
    parser.add_argument(
        "--send-dir",
        default="",
        help="允许发送的目录；默认使用临时目录",
    )
    args = parser.parse_args()

    cleanup: tempfile.TemporaryDirectory | None = None
    if args.send_dir:
        send_dir = Path(args.send_dir)
        send_dir.mkdir(parents=True, exist_ok=True)
    else:
        cleanup = tempfile.TemporaryDirectory(prefix="wechat_mcp_phase4_")
        send_dir = Path(cleanup.name)

    audit_cleanup = tempfile.TemporaryDirectory(prefix="wechat_mcp_audit_")
    audit_log = Path(audit_cleanup.name) / "audit.jsonl"

    try:
        results = await run_main_scenarios(send_dir, audit_log)
        results.append(await run_wrong_whitelist_scenario())

        # 审计文件应记录了本次操作，且不包含消息正文
        lines = (
            audit_log.read_text(encoding="utf-8").strip().splitlines()
            if audit_log.exists()
            else []
        )
        tools = {json.loads(line).get("tool") for line in lines}
        results.append(
            report(
                "10-audit-log",
                bool(lines) and {"send_file", "search_contact"} <= tools,
                f"entries={len(lines)} tools={sorted(t for t in tools if t)}",
            )
        )
    finally:
        if cleanup is not None:
            cleanup.cleanup()
        audit_cleanup.cleanup()

    passed = sum(1 for item in results if item)
    print(f"SUMMARY {passed}/{len(results)} passed", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))