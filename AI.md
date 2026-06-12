# Bogobot AI

Bogobot can use an OpenAI-compatible chat API to respond to mentions, power `/ai`, and choose Discord commands from registered AI actions.

The AI system is optional. It is configured with the top-level `ai` object in `config.json` or `local_config.json`.
Responses currently use a fixed 2048-token generation budget.

## Configuration

```json
"ai": {
    "enabled": true,
    "model": "gpt-4o-mini",
    "base_url": null,
    "api_key_env": "OPENAI_API_KEY",
    "api_key": "...",
    "custom_instruction_text": "",
    "request_interval_seconds": 60,
    "normalize_discord": true,
    "multipart_responses": true,
    "history": {
        "enabled": true,
        "path": "ai_history.sqlite3",
        "char_budget": 10000,
        "persistent_char_budget": 5000
    },
    "breaks": {
        "enabled": true,
        "active_minutes": 20,
        "break_minutes": 10
    }
}
```

Fields:

- `enabled`: Enables AI mentions and `/ai`. Defaults to `true`.
- `model`: OpenAI-compatible chat model name. Defaults to `gpt-4o-mini`.
- `base_url`: Optional OpenAI-compatible API base URL. Leave unset for the OpenAI default client endpoint.
- `api_key_env`: Environment variable containing the API key. Defaults to `OPENAI_API_KEY`.
- `api_key`: Optional API key copied into `api_key_env` at startup.
- `custom_instruction_text`: Optional admin-controlled instruction text appended after the base Bogobot instructions. Defaults to an empty string.
- `request_interval_seconds`: Minimum seconds between AI provider requests. Defaults to `60`; use `0` for local providers.
- `normalize_discord`: Annotates Discord mentions and channels with readable names before sending context to the model. Defaults to `true`.
- `multipart_responses`: Teaches the model that it may return normal text and tool calls in the same response. Defaults to `true`. When enabled, prompt examples use native tool-call JSON for context requests, memory changes, and visible-reply suppression.
- `history.enabled`: Enables per-channel short-term AI history. Defaults to `true`.
- `history.path`: SQLite path for AI history. Defaults to `ai_history.sqlite3`.
- `history.char_budget`: Per-channel character budget. Oldest stored messages are deleted first. Defaults to `10000`.
- `history.persistent_char_budget`: Global persistent memory injection budget. Defaults to `5000`.
- `breaks.enabled`: Enables AI break periods. Defaults to `true`.
- `breaks.active_minutes`: Minutes AI stays active before a break. Defaults to `20`.
- `breaks.break_minutes`: Minutes AI ignores mentions and `/ai` while on break. Defaults to `10`.

## Runtime Management

`/manage ai action:config` opens an ephemeral Components v2 panel for AI controls. It requires `ai.manage.config` and shows the base instructions, the configured custom instructions, the current AI enabled state, and break timing.

The panel can:

- Turn AI mentions and `/ai` on or off by updating `ai.enabled`.
- Edit `ai.custom_instruction_text`.
- Turn scheduled AI breaks on or off by updating `ai.breaks.enabled`.
- Edit `ai.breaks.active_minutes` and `ai.breaks.break_minutes`.

Changing break settings restarts the break timer immediately. Disabling breaks clears the current break state and restores the bot presence to online.

`/manage ai action:memory` opens an ephemeral memory portal. It requires `ai.manage.memory.channel` for current-channel history and `ai.manage.memory.persistent` for global persistent memories. The portal has tabs for channel history and persistent memory, paginates entries by stored message/memory id, supports text chunk paging for long entries, and exposes create/edit/delete controls.

For security, `/manage ai` does not expose or edit API keys, provider URLs, model names, request interval, or history path/budget settings. Runtime memory editing changes existing SQLite contents but not retention configuration.

## Local Ollama

For local Ollama with Ministral 3 8B, create a local `Modelfile` with a larger context window:

```dockerfile
FROM ministral-3:8b

PARAMETER num_ctx 16384
```

Then create the model:

```bash
ollama create bogobot-ministral -f Modelfile
```

`Modelfile` is ignored by git so local model experiments do not get committed.

Example config:

```json
"ai": {
    "enabled": true,
    "model": "bogobot-ministral",
    "base_url": "http://localhost:11434/v1",
    "api_key": "ollama",
    "custom_instruction_text": "",
    "request_interval_seconds": 0,
    "normalize_discord": true,
    "multipart_responses": true,
    "history": {
        "enabled": true,
        "path": "ai_history.sqlite3",
        "char_budget": 10000,
        "persistent_char_budget": 5000
    },
    "breaks": {
        "enabled": true,
        "active_minutes": 20,
        "break_minutes": 10
    }
}
```

## Groq

Groq can be used through its OpenAI-compatible API. A longer request interval is useful on free-tier limits.

```json
"ai": {
    "enabled": true,
    "model": "llama-3.1-8b-instant",
    "base_url": "https://api.groq.com/openai/v1",
    "api_key_env": "GROQ_API_KEY",
    "api_key": "...",
    "custom_instruction_text": "",
    "request_interval_seconds": 60,
    "normalize_discord": true,
    "multipart_responses": true,
    "history": {
        "enabled": false,
        "path": "ai_history.sqlite3",
        "char_budget": 10000,
        "persistent_char_budget": 5000
    },
    "breaks": {
        "enabled": true,
        "active_minutes": 20,
        "break_minutes": 10
    }
}
```

## Gemini

Gemini/Gemma models can be used through Google's OpenAI-compatible endpoint.

```json
"ai": {
    "enabled": true,
    "model": "gemma-4-31b-it",
    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "api_key_env": "GEMINI_API_KEY",
    "api_key": "...",
    "custom_instruction_text": "",
    "request_interval_seconds": 30,
    "normalize_discord": true,
    "multipart_responses": true,
    "history": {
        "enabled": true,
        "path": "ai_history.sqlite3",
        "char_budget": 10000,
        "persistent_char_budget": 5000
    },
    "breaks": {
        "enabled": true,
        "active_minutes": 20,
        "break_minutes": 10
    }
}
```

For Gemini/Gemma models on Google endpoints, Bogobot strips the first `<thought>...</thought>` block from model replies before recording or sending them.

## Context Format

Bogobot sends Discord metadata as XML-style context blocks with a system namespace prefix. In the examples below, `{SYSTEM_TAG}` represents `utils.ai.context.SYSTEM_NAMESPACE`, which currently defaults to `|system|`. `{ASSISTANT_TAG}` represents `utils.ai.context.ASSISTANT_NAMESPACE`, which currently defaults to `|assistant|`.

`{SYSTEM_TAG}:...` blocks are system-supplied model input. The model is instructed not to output them. User input and model output have reserved system-tag namespaces stripped before they can be treated as normal text.

Example attached metadata:

```xml
<{SYSTEM_TAG}:attached_metadata>
id: 1508656142996340787
time: 2026-05-26T02:20:53.966000+00:00
user: 1499874423019409599 Bogobot-Testing "Bogobot-Testing"
capabilities: *:100
</{SYSTEM_TAG}:attached_metadata>
```

Reply context is sent as a separate assistant-role message:

```xml
<{SYSTEM_TAG}:replied_to>
<{SYSTEM_TAG}:attached_metadata>
id: 1508656142996340787
time: 2026-05-26T02:20:53.966000+00:00
user: 1499874423019409599 Bogobot-Testing "Bogobot-Testing"
capabilities: *:100
</{SYSTEM_TAG}:attached_metadata>
previous bot message
</{SYSTEM_TAG}:replied_to>
```

Tool and command calls are recorded in history as event history:

```xml
<{SYSTEM_TAG}:event_history type="tool_use">
{"name":"ping","arguments":{}}
</{SYSTEM_TAG}:event_history>
```

Requested context is recorded and injected as an assistant-role history/context message:

```xml
<{SYSTEM_TAG}:requested_context time="2026-05-27T02:20:53.966000+00:00" type="stream">
stream_uptime: 12:03:44:10
</{SYSTEM_TAG}:requested_context>
```

Each rolling channel history message is wrapped before being sent to the model. The chat message keeps its original role; the wrapper only marks that it came from stored history.

```xml
<{SYSTEM_TAG}:message_history>
<{SYSTEM_TAG}:attached_metadata>
...
</{SYSTEM_TAG}:attached_metadata>
hello
</{SYSTEM_TAG}:message_history>
```

`capabilities` is derived from the bot account system and is emitted as a compact comma-separated `capability:depth` list. Users with no effective capabilities are shown as `none`.

```xml
<{SYSTEM_TAG}:message_history>
Hi.
</{SYSTEM_TAG}:message_history>
```

## Passive Context Requests

The model can ask Bogobot to make extra context available on a future AI turn. These requests do not run a second model call in the current turn. They are queued in SQLite, resolved before the next matching channel/user AI turn, injected as `{SYSTEM_TAG}:requested_context`, recorded into history, then discarded.

With `ai.multipart_responses` enabled, Bogobot instructs the model to use the native `request_context` tool. The model can call this tool alongside normal assistant text when it wants to answer now and queue future context in the same turn.

JSON argument examples:

```json
{"type":"stream"}
{"type":"stats"}
{"type":"sort"}
{"type":"user","payload":{"user_id":"123456789012345678"}}
{"type":"minigame","payload":{"game":"bogotree"}}
{"type":"minigame","payload":{"game":"cbogo"}}
{"type":"milestone"}
```

When `ai.multipart_responses` is disabled, Bogobot falls back to legacy hidden XML request tags in normal text replies. These are removed before sending the visible Discord reply:

```xml
<{ASSISTANT_TAG}:context_request type="stream" />
<{ASSISTANT_TAG}:context_request type="stats" />
<{ASSISTANT_TAG}:context_request type="sort" />
<{ASSISTANT_TAG}:context_request type="user" user_id="123456789012345678" />
<{ASSISTANT_TAG}:context_request type="minigame" game="bogotree" />
<{ASSISTANT_TAG}:context_request type="minigame" game="cbogo" />
<{ASSISTANT_TAG}:context_request type="milestone" />
```

The same tool is still available in legacy mode for tool-only turns:

```json
{
  "type": "minigame",
  "payload": {
    "game": "bogotree"
  }
}
```

Supported context request types:

- `stream`: Adds current stats plus sort context.
- `stats`: Adds stream uptime, stats source, last refresh time, and the cached stream stats.
- `sort`: Adds section count, current color-derived sort values, and current best-shuffle section state.
- `user`: Resolves a Discord user id into known username/display name/member metadata and account capabilities.
- `minigame`: Adds server-local compact state and account entries for `bogotree` or `cbogo`.
- `milestone`: Adds all milestone names, current values, and recent text history. Images are intentionally omitted.

Queued context requests are channel-scoped. If a request also has a `user_id`, it is only consumed by that user's next matching AI turn.

## Persistent Memory

The model can maintain global persistent memory in SQLite. The configured memory budget is injected each turn, along with the remaining character count. Memories are intended for durable preferences, stable project facts, recurring decisions, and useful semi-persistent working context.

With `ai.multipart_responses` enabled, Bogobot instructs the model to use the native `persistent_memory` tool. The model can call this tool alongside normal assistant text when it wants to answer now and update memory in the same turn.

JSON argument examples:

```json
{"operation":"create","content":"The user prefers concise answers."}
{"operation":"edit","id":123,"content":"The user prefers concise answers with concrete examples."}
{"operation":"remove","id":123}
```

If a create or edit would exceed `ai.history.persistent_char_budget`, Bogobot records the attempt with `failed=true` and does not create or edit the stored memory.

When `ai.multipart_responses` is disabled, Bogobot falls back to legacy hidden assistant XML tags for memory changes in normal text replies.

With `ai.multipart_responses` enabled, Bogobot exposes a native `dont_respond` tool. The model can call it by itself or alongside normal assistant text and other tool calls. If normal assistant text is included with `dont_respond`, that text is recorded in history but not displayed in Discord. Inline assistant XML controls are disabled in multipart mode and are treated as normal text.

When `ai.multipart_responses` is disabled, the model can include `<{ASSISTANT_TAG}:dont_respond />` to suppress the visible Discord reply while still leaving the turn in history.

## AI Actions

Plugins register AI-callable actions with `@utils.ai.action(...)`.
The decorator is normally stacked with a slash command decorator so the same coroutine can be invoked by Discord users or by the AI command runner.

```python
from utils.ai import AIParam, action

@action(
    "ping",
    "Show bot latency.",
    params={"user": AIParam("Discord user id.", type=discord.User | discord.Member | None, required=False)},
)
async def ping(interaction: discord.Interaction, user: discord.User | discord.Member | None = None):
    ...
```

The first argument is the AI action name. It can contain spaces, such as `"bogo roll"`; the OpenAI tool name is generated automatically by lowercasing and replacing non-word characters with underscores. If two actions would produce the same tool name, Bogobot adds a numeric suffix.

`command_name` defaults to the action name and is used for telemetry/error context. Set it when the AI action name should differ from the Discord command name. Action metadata such as `capabilities=[...]` is passed as decorator keyword arguments, then checked by the normal command runner.

Command parameters are declared with `AIParam`. Supported parameter types are:

- `str`, `int`, `float`, `bool`, and `object`.
- `Literal["a", "b"]` with string choices, exposed as an enum.
- `discord.User`, `discord.Member`, or a union of those with `None`.

`AIParam(required=False, default=...)` makes an argument optional in the tool schema. Unknown arguments, missing required arguments, invalid enum choices, and values that cannot be coerced are rejected before the command runs. Discord user parameters accept a user ID or mention and resolve only from visible guild/message/client cache context.

Registered actions are exposed to the model as OpenAI-compatible tools. A tool call runs the matching Discord command through the normal command runner. Plain model text becomes a conversational Discord reply.
