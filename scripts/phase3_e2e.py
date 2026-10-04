"""阶段 3 端到端测试：以真实 MCP stdio 协议驱动 Server，覆盖计划书测试场景。

用法（项目根目录，需微信已登录且主窗口可见）::

    # 场景 1/4/5/6/7 可独立完成；场景 2/3 需在 --wait-inbound 窗口内由他方账号发消息
    .venv\\Scripts\\python.exe scripts\\phase3_e2e.py --wait-inbound 60

输出每行形如 ``RESULT <场景> PASS|FAIL <详情>``，便于记录测试结果。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client

SELF_TARGET = "文件传输助手"
BOGUS_PATH = r"F:\__no_such_project__"


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


async def run_main_scenarios(wait_inbound: float) -> list[bool]:
    results: list[bool] = []
    params = server_params()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            expected = ["get_chat_history", "get_chat_list", "get_wechat_status", "send_message"]
            results.append(report("0-tool-discovery", names == expected, f"tools={names}"))

            # 场景 1：查询微信状态
            status = _payload(await session.call_tool("get_wechat_status", {}))
            results.append(
                report(
                    "1-status",
                    bool(status.get("ok")) and status.get("state") == "connected",
                    f"state={status.get('state')} backend={status.get('backend')}",
                )
            )

            # 场景 2/3 需要监听窗口内有他方入站消息
            if wait_inbound > 0:
                print(
                    f"WAITING_INBOUND up to {wait_inbound}s ..."
                    "（请在此期间用他方账号发一条消息，收到即继续）",
                    flush=True,
                )
                loop = asyncio.get_running_loop()
                deadline = loop.time() + wait_inbound
                while loop.time() < deadline:
                    await asyncio.sleep(1.0)
                    probe = _payload(
                        await session.call_tool("get_chat_list", {"limit": 1})
                    )
                    if probe.get("chats"):
                        print("INBOUND_RECEIVED", flush=True)
                        break

            listing = _payload(await session.call_tool("get_chat_list", {"limit": 10}))
            chats = listing.get("chats") or []
            results.append(
                report(
                    "2-chat-list",
                    bool(listing.get("ok")),
                    f"total={listing.get('total')} names={[c.get('name') for c in chats]}",
                )
            )

            if chats:
                chat_name = chats[0]["name"]
                history = _payload(
                    await session.call_tool(
                        "get_chat_history", {"chat_name": chat_name, "limit": 5}
                    )
                )
                results.append(
                    report(
                        "3-chat-history",
                        bool(history.get("ok")) and bool(history.get("messages")),
                        f"chat={chat_name} count={len(history.get('messages') or [])}",
                    )
                )
            else:
                results.append(
                    report("3-chat-history", False, "缓冲区无会话，需他方入站消息")
                )

            # 场景 4：向明确指定的测试目标发送文本（使用文件传输助手，仅发给自己）
            sent = _payload(
                await session.call_tool(
                    "send_message",
                    {"recipient": SELF_TARGET, "message": "[phase3 e2e] 场景4 测试文本"},
                )
            )
            results.append(
                report(
                    "4-send-message",
                    bool(sent.get("ok")) and sent.get("status") == "sent",
                    f"status={sent.get('status')} resolved={sent.get('resolved_recipient')}",
                )
            )

            # 场景 5a：空目标被拒绝
            empty = _payload(
                await session.call_tool("send_message", {"recipient": "   ", "message": "x"})
            )
            results.append(
                report(
                    "5a-reject-empty-target",
                    (not empty.get("ok")) and empty.get("error", {}).get("code") == "target_invalid",
                    f"code={empty.get('error', {}).get('code')}",
                )
            )

            # 场景 5b：目标不唯一时拒绝（基于缓冲区会话名构造部分匹配）
            ambiguous_ok, ambiguous_detail = await _check_ambiguity(session, chats)
            results.append(report("5b-reject-ambiguous", ambiguous_ok, ambiguous_detail))

            # 场景 7：连续多次调用不串扰（顺序与结果保持一致）
            order = []
            for index in range(3):
                outcome = _payload(
                    await session.call_tool(
                        "send_message",
                        {"recipient": SELF_TARGET, "message": f"[phase3 e2e] 连续调用 {index}"},
                    )
                )
                order.append(bool(outcome.get("ok")) and outcome.get("status") == "sent")
            results.append(
                report("7-sequential-no-crosstalk", all(order), f"results={order}")
            )

    return results


async def _check_ambiguity(session: ClientSession, chats: list[dict]) -> tuple[bool, str]:
    """若缓冲区存在多个可被同一子串命中的会话，则验证其被拒绝。"""
    names = [c["name"] for c in chats]
    if len(names) < 2:
        return True, f"skip(仅 {len(names)} 个会话，歧义场景由单元测试覆盖)"

    # 找一个能同时部分匹配多个会话的子串
    probe = ""
    for length in range(2, 6):
        for name in names:
            candidate = name[:length]
            if candidate and sum(candidate in other for other in names) > 1:
                probe = candidate
                break
        if probe:
            break
    if not probe:
        return True, f"skip(无共享子串，names={names})"

    result = _payload(
        await session.call_tool("send_message", {"recipient": probe, "message": "x"})
    )
    ok = (not result.get("ok")) and result.get("error", {}).get("code") == "target_ambiguous"
    return ok, f"probe={probe} code={result.get('error', {}).get('code')} " \
               f"candidates={result.get('error', {}).get('detail', {}).get('candidates')}"


async def run_error_path_scenario() -> bool:
    """场景 6：后端不可用时返回结构化错误，而非虚报成功。"""
    params = server_params({"DEEPSEEKGIRL_PATH": BOGUS_PATH})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            status = _payload(await session.call_tool("get_wechat_status", {}))
    error = status.get("error") or {}
    return report(
        "6-backend-unavailable",
        (not status.get("ok")) and error.get("code") == "backend_unavailable",
        f"ok={status.get('ok')} code={error.get('code')}",
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="wechat-mcp 阶段3 端到端测试")
    parser.add_argument(
        "--wait-inbound",
        type=float,
        default=0.0,
        help="为场景2/3等待他方入站消息的秒数",
    )
    args = parser.parse_args()

    results = await run_main_scenarios(args.wait_inbound)
    results.append(await run_error_path_scenario())

    passed = sum(1 for item in results if item)
    print(f"SUMMARY {passed}/{len(results)} passed", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))