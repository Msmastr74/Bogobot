# Capabilities

Capabilities are string permissions stored on each account. A user can run a command when their effective account permissions include every capability required by that command.

- `*`: Owner-level wildcard. Grants every capability.
- `commands.*`: Default user capability. Grants normal command access.
- `user.*`: Default user capability. Grants normal user features.
- `commands.<qualified.command.name>`: Automatically registered for every slash command. Command names use the Discord qualified name with spaces converted to dots, such as `commands.manage.raid`.
- `grant.<capability>`: Allows granting or revoking a capability if the account also has enough delegation depth for the target capability.
- `grant.[any]`: Allows using the accounts capability management command for any capability target.
- `server.<capability>`: Capability-management target prefix. It writes the capability to the current server's local account record, so `server.raid.manage` grants local `raid.manage`.
- `[any]`: Special matcher segment for exactly one capability segment.
- `[all]`: Special matcher segment for the rest of a capability path.

`server.<capability>` is not checked directly by commands. It is only used while granting, revoking, filtering, or expanding presets. Runtime permission checks still use the unprefixed capability on the server-local account view. For example, `/accounts capability action:grant capabilities:server.discord.announce user:@user` stores `discord.announce` under that user's current-server permissions, and `/manage announce` then sees `discord.announce` through `account.local(guild_id)`.

## Managing Capabilities

- `/accounts capability action:grant capabilities:a,b,c user:@user`: Grants a comma-separated capability list.
- `/accounts capability action:revoke capabilities:a,b,c user:@user`: Revokes a comma-separated capability list.
- `/accounts capability action:reset user:@user`: Atomically resets a user's global capabilities to defaults and clears local permission overrides. It fails without changing anything if the caller cannot revoke every current capability and grant every default capability.
- `/accounts capability action:grant preset:name user:@user`: Grants every capability in a preset.
- `/accounts capability action:revoke preset:name user:@user`: Revokes every capability in a preset.
- `/accounts preset action:show name:name`: Shows the expanded capability list for a preset.
- `/accounts preset action:create name:name capabilities:a,b,c`: Creates or replaces a custom global preset.
- `/accounts preset action:remove name:name`: Removes a custom global preset.
- Global presets: `default`, `user`, `ai`, `moderator`, `admin`.
- Server-local presets: `server.default`, `server.user`, `server.ai`, `server.moderator`, `server.admin`.

Custom presets are stored globally in config under `account_capability_presets`. Custom preset definitions must contain unprefixed capabilities; use `server.<preset>` when applying one server-locally.

## Account

- `accounts.ban`: Ban or unban accounts from bot commands.
- `capabilities.manage_presets`: Create, remove, or show account capability presets.

Account info and account listing are intentionally public through normal `commands.*` access for transparency.

## AI

- `ai.manage`: Manage AI settings.
- `ai.activity.manage`: Schedule or remove AI activities.
- `ai.activity.trigger`: Trigger an AI activity immediately.
- `user.ai`: Use user-facing AI entry points, including `/ai` and mention/reply AI chat.

## Archive

- `archive.manage`: Manage visual stream archive recording.

## Discord

- `discord.announce`: Send or edit bot announcements.
- `discord.message`: Manage Discord messages through the bot.

## Games

- `games.bogotree.reset`: Reset bogotree state.
- `games.cbogo.reset`: Reset cbogo state and scores.
- `games.cbogo.reset_last_user`: Clear cbogo's last-user state.

## Milestones

- `milestones.info`: View milestone history.
- `milestones.manage`: Manage milestone subscriptions, spoofing, and rate-limit state.

## Raid Protection

- `raid.exempt`: Exempt a member from raid quarantine checks.
- `raid.manage`: Manage raid protection.
- `raid.unquarantine`: Use raid unquarantine controls.

## System

- `system.loglevel`: Temporarily change runtime log levels.
- `system.logs`: View bot logs or write a user log entry.
- `system.state`: Show or change bot process state.

## Telemetry

- `telemetry.view`: View recent bot command activity.

## Verification

- `verification.manage`: Create verification messages.
