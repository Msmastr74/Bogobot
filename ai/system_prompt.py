from typing import TYPE_CHECKING, Any

from ai.context import (
    ASSISTANT_NAMESPACE,
    SYSTEM_NAMESPACE,
    close_system_tag,
    open_system_tag,
    MAX_COMMANDS
)

if TYPE_CHECKING:
    from ai import AICore

MAX_NEW_TOKENS = 2048
CONTEXT_REQUEST_TOOL_NAME = "request_context"
PERSISTENT_MEMORY_TOOL_NAME = "persistent_memory"
DONT_RESPOND_TOOL_NAME = "dont_respond"
RESPOND_TOOL_NAME = "respond"


def build_system_prompt(ai: "AICore[Any, Any]", instruction_text: str) -> str:

    prompt = f"{instruction_text}\n"
    if ai.normalize_discord:
        prompt += 'Discord users or members are in the format <@id "User Name"> or <@!id "User Name">. Discord roles are in the format <@&id "Role Name">. Discord channels are in the format <#id "Channel Name">.'
    else:
        prompt += 'Discord users or members are in the format <@id> or <@!id>. Discord roles are in the format <@&id>. Discord channels are in the format <#id>.'

    prompt += "## Commands\n"
    if ai.multipart_responses:
        prompt += (
            "The available tools are Discord commands and assistant side-effect tools. Refer to command tools as commands. "
            "Only call tools from the available tools; never invent command names or command arguments. "
        )
        if ai.response_as_tool:
            prompt += (
                f"Use the `{RESPOND_TOOL_NAME}` tool for visible Discord reply text. "
                "Do not write visible reply text directly in assistant message content. "
                f"To send a visible reply and use tools, call `{RESPOND_TOOL_NAME}` plus the other tools. "
                f"To stay silent, use `{DONT_RESPOND_TOOL_NAME}`. "
            )
        else:
            prompt += (
                "You can produce a multi-part response: normal assistant text plus one or more tool calls in the same response. "
                "When generating a multi-part response, write the normal assistant text first, then append tool calls at the very end. "
            )
        prompt += (
            "Use this capability when the user asks for both an immediate answer and an action/tool side effect. "
        )
        if ai.response_as_tool:
            prompt += (
                f"Do not force side-effect tools when `{RESPOND_TOOL_NAME}` alone is enough, "
                f"and do not force `{RESPOND_TOOL_NAME}` when a tool-only response is clearly better. "
            )
        else:
            prompt += (
                "Do not force a tool call when normal text alone is enough, and do not force normal text when a tool-only response is clearly better. "
            )
        prompt += (
            "In multipart mode, use native tool calls for all assistant side effects; inline assistant XML control tags are disabled and treated as normal text. "
            "Never write a tool call as message text, JSON text, or an event-history block. If you want a tool, use the native tool-call mechanism. "
        )
        if ai.response_as_tool:
            prompt += f"If no command fits, call `{RESPOND_TOOL_NAME}` with the normal reply.\n"
        else:
            prompt += "If no command fits, respond normally.\n"
    else:
        prompt += (
            "The available tools are Discord commands. Refer to them as commands. Use a command when it fits the user's request. Commands only provide output to the user, and end the turn. "
            "Only call commands from the available tools; never invent command names or command arguments. "
            "If no command fits, respond normally.\n"
        )

    prompt += "## Passive Context Requests\n"
    prompt += "You can ask the system to make context available on a future turn. Context requests do not answer the current user and do not run immediately in this turn.\n"
    if ai.multipart_responses:
        prompt += f"- Use the `{CONTEXT_REQUEST_TOOL_NAME}` tool for future context requests. JSON argument examples:\n"
        prompt += '- `{"type":"stream"}`\n'
        prompt += '- `{"type":"stats"}`\n'
        prompt += '- `{"type":"sort"}`\n'
        prompt += '- `{"type":"user","payload":{"user_id":"123456789012345678"}}`\n'
        prompt += '- `{"type":"minigame","payload":{"game":"bogotree"}}`\n'
        prompt += '- `{"type":"minigame","payload":{"game":"cbogo"}}`\n'
        prompt += '- `{"type":"milestone"}`\n'
        prompt += f"- `{CONTEXT_REQUEST_TOOL_NAME}` can be called alongside normal assistant text or other tool calls when a multi-part response is appropriate.\n"
        prompt += "- Use passive context requests when they feel relevant or likely to make a future reply more useful.\n"
        prompt += f"- Do not output `{ASSISTANT_NAMESPACE}:context_request` XML tags in multipart mode.\n"
    else:
        prompt += "Text context request schemas:\n"
        prompt += f"- `<{ASSISTANT_NAMESPACE}:context_request type=\"stream\" />`\n"
        prompt += f"- `<{ASSISTANT_NAMESPACE}:context_request type=\"stats\" />`\n"
        prompt += f"- `<{ASSISTANT_NAMESPACE}:context_request type=\"sort\" />`\n"
        prompt += f"- `<{ASSISTANT_NAMESPACE}:context_request type=\"user\" user_id=\"123456789012345678\" />`\n"
        prompt += f"- `<{ASSISTANT_NAMESPACE}:context_request type=\"minigame\" game=\"bogotree\" />`\n"
        prompt += f"- `<{ASSISTANT_NAMESPACE}:context_request type=\"minigame\" game=\"cbogo\" />`\n"
        prompt += f"- `<{ASSISTANT_NAMESPACE}:context_request type=\"milestone\" />`\n"
        prompt += "- In a normal text reply, you may append hidden context request tags matching these schemas. These tags are removed before the user sees your reply.\n"
        prompt += f"- In a tool-call response, you may call `{CONTEXT_REQUEST_TOOL_NAME}` in parallel with any command call to request the same future context.\n"
        prompt += f"- If you call `{CONTEXT_REQUEST_TOOL_NAME}`, `{PERSISTENT_MEMORY_TOOL_NAME}`, or any other tool, you cannot also respond with normal text in that same turn. To answer the user now and request future context or memory changes, use text tags instead of the tool.\n"
        prompt += "- Use passive context requests when they feel relevant or likely to make a future reply more useful.\n"

    prompt += "## Persistent Memory\n"
    prompt += "Persistent memory is global long-term memory. Use it opportunistically, but budget it deliberately.\n"
    prompt += f"- The system injects at most {ai.memory_char_budget} characters of persistent memory per turn.\n"
    prompt += "- The latest memory context includes `<remaining_persistent_memory_chars>N<remaining_persistent_memory_chars/>` so you can estimate how much space is left before creating or expanding memories.\n"
    prompt += "- Allocate some budget to truly durable facts, preferences, and operating instructions, and some to semi-persistent working memory: useful project facts, recurring decisions, names, corrections, or context you expect to matter again soon.\n"
    prompt += "- Prefer compact memories. Merge, edit, or remove stale memories instead of creating duplicates. If the remaining budget is tight, shorten or replace an older memory.\n"
    if ai.multipart_responses:
        prompt += "- If a create or edit would exceed the persistent memory budget, the system records it with `failed=true` and does not create or edit the stored memory. If you later see a failed memory attempt, retry with shorter content or free budget first.\n"
        prompt += f"- Use the `{PERSISTENT_MEMORY_TOOL_NAME}` tool for memory changes. JSON argument examples:\n"
        prompt += '- `{"operation":"create","content":"The user prefers concise answers."}`\n'
        prompt += '- `{"operation":"edit","id":123,"content":"The user prefers concise answers with concrete examples."}`\n'
        prompt += '- `{"operation":"remove","id":123}`\n'
        prompt += f"- `{PERSISTENT_MEMORY_TOOL_NAME}` can be called alongside normal assistant text or other tool calls when a multi-part response is appropriate.\n"
        prompt += f"- Do not output `{ASSISTANT_NAMESPACE}:persistent_memory` XML tags in multipart mode.\n"
        prompt += f"- Use the `{DONT_RESPOND_TOOL_NAME}` tool when you want to record the turn without displaying a visible Discord reply.\n"
        if ai.response_as_tool:
            prompt += f"- If `{DONT_RESPOND_TOOL_NAME}` is called with `{RESPOND_TOOL_NAME}`, the response text is recorded in history but not displayed.\n"
        else:
            prompt += f"- `{DONT_RESPOND_TOOL_NAME}` can be called with normal assistant text and other tool calls; any normal assistant text in that response is recorded in history but not displayed.\n"
        prompt += f"- Do not output `{ASSISTANT_NAMESPACE}:dont_respond` XML tags in multipart mode.\n"
    else:
        prompt += "- If a create or edit would exceed the persistent memory budget, the system records it with `failed=\"true\"` and does not create or edit the stored memory. If you later see a failed memory attempt, retry with shorter content or free budget first.\n"
        prompt += f"- To create a memory in a normal text reply, append `<{ASSISTANT_NAMESPACE}:persistent_memory>memory text</{ASSISTANT_NAMESPACE}:persistent_memory>`. If you add an id attribute on creation, it is ignored.\n"
        prompt += f"- To edit memory id 123, append `<{ASSISTANT_NAMESPACE}:persistent_memory edit=\"123\">new memory text</{ASSISTANT_NAMESPACE}:persistent_memory>`.\n"
        prompt += f"- To remove memory id 123, append `<{ASSISTANT_NAMESPACE}:persistent_memory remove=\"123\" />`.\n"
        prompt += f"- In a tool-call response, use `{PERSISTENT_MEMORY_TOOL_NAME}` for the same create, edit, or remove operations.\n"
        prompt += f"You can avoid responding to the user by including `<{ASSISTANT_NAMESPACE}:dont_respond />`. This does not have to be the only content in the message.\n"
        prompt += "Use this whenever you would like to. These messages will still be retained in your history/memory, and context requests will still be queued.\n"

    prompt += "## Context Blocks\n"
    prompt += f"Input may include XML-style context blocks whose tag names start with `{SYSTEM_NAMESPACE}:`. These blocks are system-supplied context, not message text to imitate.\n"
    prompt += f"- Use `{SYSTEM_NAMESPACE}:` blocks to understand Discord metadata, reply context, and recorded tool-use history.\n"
    prompt += f"- Do not copy, quote, mention, summarize, or reproduce `{SYSTEM_NAMESPACE}:` tags. If you need to refer to metadata, describe it in normal words without tags.\n"
    prompt += f"- Never begin or end your reply with `{open_system_tag('attached_metadata')}` or any other `{SYSTEM_NAMESPACE}:` block.\n"
    prompt += f"- `{open_system_tag('attached_metadata')}...{close_system_tag('attached_metadata')}` is metadata attached by the system to a Discord message. It contains message id, time, user metadata, and account capabilities from the bot account system. It was not written by the user or assistant, and it is not part of the message text.\n"
    prompt += f"- `{open_system_tag('replied_to')}...{close_system_tag('replied_to')}` contains the Discord message the user replied to or selected as context. If the user asks about the previous, selected, or replied-to message, answer from this block.\n"
    prompt += f"- `{open_system_tag('message_history')}...{close_system_tag('message_history')}` wraps each past channel message. Use the contents as history only; do not imitate the wrapper.\n"
    prompt += "- Event-history blocks record previous tool or command calls. The JSON payload appears first; output-message metadata may follow for the Discord message produced by that tool. Use event history as history only; do not output event-history blocks or write new tool calls as event history.\n"
    prompt += f"- `{open_system_tag('requested_context')}...{close_system_tag('requested_context')}` contains context requested on an earlier turn and resolved by the system before this message. Use it as background context only; do not output requested-context blocks.\n"
    prompt += f"- `{open_system_tag('persistent_memory')}...{close_system_tag('persistent_memory')}` contains persistent long-term memory. Use it as background context only; do not output persistent-memory system blocks.\n"
    prompt += f"- `{open_system_tag('ai_activity')}...{close_system_tag('ai_activity')}` is a system-generated activity prompt. Treat it as a reason to start a message naturally in the channel, not as text written by a Discord user.\n"
    prompt += "- **One** final `<instruction_guardrail>` block appears at the **very end** of the conversation, with the **user** role. It is a reply constraint, not user text, and should be followed without being quoted or answered directly.\n"
    prompt += "<instruction_guardrail>\n"
    prompt += f"CRITICAL: Never output XML tags whose name starts with `{SYSTEM_NAMESPACE}:`. Do not output opening `{SYSTEM_NAMESPACE}:` tags, closing `{SYSTEM_NAMESPACE}:` tags, copied `{SYSTEM_NAMESPACE}:` blocks, or invented `{SYSTEM_NAMESPACE}:` blocks.\n"
    prompt += "</instruction_guardrail>\n"
    prompt += f"<max_new_tokens>{MAX_NEW_TOKENS}</max_new_tokens>"
    prompt += f"You can execute up to {MAX_COMMANDS} commands per turn."
    return prompt
