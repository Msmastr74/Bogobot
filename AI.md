# Bogobot AI

Bogobot can use an OpenAI-compatible chat API to respond to mentions, power `/ai`, and choose Discord commands from registered AI actions.

The AI system is optional. It is configured with the top-level `ai` object in `config.json` or `local_config.json`.

## Configuration

```json
"ai": {
    "enabled": true,
    "model": "gpt-4o-mini",
    "base_url": null,
    "api_key_env": "OPENAI_API_KEY",
    "api_key": "...",
    "request_interval_seconds": 60,
    "normalize_discord": true,
    "history": {
        "enabled": true,
        "path": "ai_history.sqlite3",
        "char_budget": 10000
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
- `request_interval_seconds`: Minimum seconds between AI provider requests. Defaults to `60`; use `0` for local providers.
- `normalize_discord`: Annotates Discord mentions and channels with readable names before sending context to the model. Defaults to `true`.
- `history.enabled`: Enables per-channel short-term AI history. Defaults to `true`.
- `history.path`: SQLite path for AI history. Defaults to `ai_history.sqlite3`.
- `history.char_budget`: Per-channel character budget. Oldest stored messages are deleted first. Defaults to `10000`.
- `breaks.enabled`: Enables AI break periods. Defaults to `true`.
- `breaks.active_minutes`: Minutes AI stays active before a break. Defaults to `20`.
- `breaks.break_minutes`: Minutes AI ignores mentions and `/ai` while on break. Defaults to `10`.

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
    "request_interval_seconds": 0,
    "normalize_discord": true,
    "history": {
        "enabled": true,
        "path": "ai_history.sqlite3",
        "char_budget": 10000
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
    "request_interval_seconds": 60,
    "normalize_discord": true,
    "history": {
        "enabled": false,
        "path": "ai_history.sqlite3",
        "char_budget": 10000
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
    "request_interval_seconds": 30,
    "normalize_discord": true,
    "history": {
        "enabled": true,
        "path": "ai_history.sqlite3",
        "char_budget": 10000
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

Bogobot sends Discord metadata as XML-style context blocks with a system namespace prefix. In the examples below, `{SYSTEM_TAG}` represents `utils.ai_context.SYSTEM_NAMESPACE`, which currently defaults to `system`. `{ASSISTANT_TAG}` represents `utils.ai_context.ASSISTANT_NAMESPACE`, which currently defaults to `assistant`.

`{SYSTEM_TAG}:...` blocks are system-supplied model input. The model is instructed not to output them. User input and model output have reserved system-tag namespaces stripped before they can be treated as normal text.

Example attached metadata:

```xml
<{SYSTEM_TAG}:attached_metadata>
id: 1508656142996340787
time: 2026-05-26T02:20:53.966000+00:00
user: 1499874423019409599 Bogobot-Testing "Bogobot-Testing"
</{SYSTEM_TAG}:attached_metadata>
```

Reply context is sent as a separate assistant-role message:

```xml
<{SYSTEM_TAG}:replied_to>
<{SYSTEM_TAG}:attached_metadata>
id: 1508656142996340787
time: 2026-05-26T02:20:53.966000+00:00
user: 1499874423019409599 Bogobot-Testing "Bogobot-Testing"
</{SYSTEM_TAG}:attached_metadata>
previous bot message
</{SYSTEM_TAG}:replied_to>
```

Command calls are recorded in history like this:

```xml
<{SYSTEM_TAG}:command>{"name":"ping","arguments":{}}</{SYSTEM_TAG}:command>
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

```xml
<{SYSTEM_TAG}:message_history>
Hi.
</{SYSTEM_TAG}:message_history>
```

## Passive Context Requests

The model can ask Bogobot to make extra context available on a future AI turn. These requests do not run a second model call in the current turn. They are queued in SQLite, resolved before the next matching channel/user AI turn, injected as `{SYSTEM_TAG}:requested_context`, recorded into history, then discarded.

For normal text replies, the model can append hidden XML request tags. These are removed before sending the visible Discord reply:

```xml
<{ASSISTANT_TAG}:context_request type="stream" />
<{ASSISTANT_TAG}:context_request type="user" user_id="123456789012345678" />
<{ASSISTANT_TAG}:context_request type="minigame" game="bogotree" />
<{ASSISTANT_TAG}:context_request type="minigame" game="cbogo" />
<{ASSISTANT_TAG}:context_request type="milestone" />
```

For tool-call turns, Bogobot also exposes a `request_context` tool:

```json
{
  "type": "minigame",
  "payload": {
    "game": "bogotree"
  }
}
```

The tool version is useful when the model is already making tool calls. If the model needs to answer with normal text and request future context, it should use the hidden XML form instead, because tool-call turns cannot also send normal text reliably.

Supported context request types:

- `stream`: Adds current stream/bot state such as stream uptime.
- `user`: Resolves a Discord user id into known username/display name/bot metadata.
- `minigame`: Adds compact state for `bogotree` or `cbogo`.
- `milestone`: Adds all milestone names, current values, and recent text history. Images are intentionally omitted.

## AI Actions

Plugins register AI-callable actions with `@utils.ai.action(...)`.

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

Action metadata such as `perm_requirement` is passed as decorator keyword arguments. Command parameters are declared with `AIParam`; unsupported or invalid model arguments are rejected before the command runs.

Registered actions are exposed to the model as OpenAI-compatible tools. A tool call runs the matching Discord command through the normal command runner. Plain model text becomes a conversational Discord reply.
