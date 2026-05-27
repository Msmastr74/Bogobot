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

Bogobot sends Discord metadata as XML-style context blocks with a system namespace prefix. In the examples below, `{SYSTEM_TAG}` represents `utils.ai.SYSTEM_NAMESPACE`, which currently defaults to `system`. These blocks are model input only.

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
