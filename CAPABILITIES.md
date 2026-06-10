# Capabilities

Capabilities are string permissions stored on each account. A user can run a command when their effective account permissions include every capability required by that command.

## Account Targets

Capabilities can be stored on user, role, and guild account rows. Guild account rows exist for future use; runtime permission checks currently use user and role rows.

User accounts are the normal access pattern. A global user account stores default/global capabilities, and a server-local user account stores per-server overrides.

Role accounts are always server-local. Granting capabilities to a role target stores those capabilities on that role's row for the current server. A member's effective server-local user account is built in this order:

- Global user account.
- Current-server role accounts for cached roles on that member.
- Current-server user account.

If multiple inherited records contain the same capability key, the effective account keeps the highest depth. If different capability keys match the same runtime check, the check also uses the highest matching depth. If the bot cannot resolve the guild or member from cache, role accounts are ignored for that check.

## Depths

Each stored capability has an integer depth. Depth controls delegation authority, not basic use.

- Missing capability: cannot use it and cannot delegate it.
- Depth `0`: can use the capability, but cannot grant or revoke it for anyone else.
- Depth `1`: can grant or revoke that capability only at depth `0`.
- Depth `N`: can grant or revoke that capability only at depths lower than `N`.

In other words, grant/revoke checks are strict: the caller's effective `.grant` depth must be greater than the target depth. This prevents users from creating equal peers. For example, `raid.grant: 1` can grant `raid` or `raid.use` at depth `0`, while `raid.grant: 50` can grant depths `0` through `49`.

The same depth number is used for `.use` and `.grant`, but they are checked differently:

- `.use`: depth `0` or higher is enough to use a capability.
- `.grant`: must be greater than the depth being granted, revoked, or reset.

`[all]` at a high depth is owner-like authority over every capability. `banned` disables all other capabilities regardless of depth.

## Syntax

- `[all]`: Owner-level wildcard. Grants every capability.
- `commands`: Default user capability. Grants normal command access.
- `user`: Default user capability. Grants normal user features.
- `commands.<qualified.command.name>`: Automatically registered for every slash command. Command names use the Discord qualified name with spaces converted to dots, such as `commands.manage.raid`.
- `<capability>`: Root capability. Grants that exact capability and capabilities below it, for both `.use` and `.grant` operations.
- `<capability>.use`: Operation-specific use permission for that capability and capabilities below it.
- `<capability>.grant`: Operation-specific grant/revoke permission for that capability and capabilities below it.
- `server.<capability>`: Capability-management target prefix. It writes the capability to the current server's local account record, so `server.raid.manage` grants local `raid.manage`.
- `(preset)`: Dynamic preset segment. It expands to the current capability list for `preset` whenever permissions are checked.
- `server.(preset)`: Dynamic server-local preset segment. It writes a distinct server preset reference to the current server's local account record. It resolves `server.preset` first, then falls back to `preset` when no server-specific preset exists. The `preset` portion must contain only letters, numbers, and `_`.
- `banned`: Negative capability. When present on an effective account, all other capabilities are disabled. It is reserved and can only be changed by `/accounts ban`.
- `[any]`: Applied wildcard segment. When checking whether a wildcard grant/revoke/reset is allowed, requiring `[any]` means the caller only needs authority over one matching registered capability.
- `[all]`: Applied wildcard segment. When checking whether a wildcard grant/revoke/reset is allowed, requiring `[all]` means the caller needs authority over every matching registered capability. A root `[all]` grants every capability.

Root capabilities are prefix matches. For example, `commands` matches `commands`, `commands.ping`, and `commands.manage.raid`. A more specific capability, such as `commands.manage`, matches itself and descendants but not lower prefixes such as `commands`.

Use `.use` and `.grant` when you need different effective depths. For example, `raid.use: 5` and `raid.grant: 1` let a user use raid capabilities more broadly than they can delegate them. Granting, revoking, or resetting a capability uses the target account's exact maximum depth for that capability plus its `.use` and `.grant` forms. Broader prefixes can coexist with lower specific capabilities; account capability displays label lower entries when a broader entry overrides them.

`server.<capability>` is not checked directly by commands. It is only used while granting, revoking, filtering, or expanding presets. Runtime permission checks still use the unprefixed capability on the server-local account view. For example, `/accounts capabilities action:grant capabilities:server.discord.announce target:@user` stores `discord.announce` under that user's current-server permissions, and `/manage announce` then sees `discord.announce` through `account.local(guild_id)`.

Role targets are implicitly server-local. For example, `/accounts capabilities action:grant target:@Moderators capabilities:discord.announce` stores `discord.announce` on the `@Moderators` role account in the current server.

Preset references are stored dynamically instead of being flattened. For example, granting `(moderator)` means later edits to the `moderator` preset automatically affect users with that capability. Granting `server.(moderator)` stores a separate server-local preset reference; if a `server.moderator` preset exists, it is used instead of `moderator`. A preset segment that does not exist expands to nothing and matches nothing.

Preset expansion canonicalizes prefixes and suffixes. Repeated `server.` prefixes are collapsed during resolution, and repeated operation suffixes collapse to one suffix, so a server preset containing `server.raid.use` still resolves as `raid.use` when applied through `server.(...)`.

Global management capabilities can affect global or server-local targets. Server-local management capabilities only affect server-local targets. For example, `accounts.ban` can ban globally or in a server, while `server.accounts.ban` can only ban in the current server. Likewise, `server.capabilities.manage` can use `/accounts capabilities` only for `server.*` grants and revokes.

## Managing Capabilities

- `/accounts capabilities action:grant target:@user capabilities:a,b,c`: Grants a comma-separated capability list.
- `/accounts capabilities action:grant target:@role capabilities:a,b,c`: Grants a comma-separated capability list to a server-local role account.
- `/accounts capabilities action:revoke target:@user capabilities:a,b,c`: Revokes a comma-separated capability list.
- `/accounts capabilities action:revoke target:@role capabilities:a,b,c`: Revokes a comma-separated capability list from a server-local role account.
- `/accounts capabilities action:reset target:@user`: Atomically resets a user's global capabilities to defaults and clears local permission overrides. It fails without changing anything if the caller cannot revoke every current capability and grant every default capability.
- `/accounts capabilities action:reset target:@role`: Atomically clears a role's server-local capabilities except reserved capabilities.
- `/accounts capabilities action:resolve capabilities:a,b,c`: Shows the concrete capabilities produced by the comma-separated capability list.
- `/accounts capabilities action:show target:@user`: Shows the user's effective capability list. If `target` is omitted, shows the caller.
- `/accounts capabilities action:show target:@role`: Shows the role's effective server-local capability list.
- `/accounts capabilities action:grant target:@user capabilities:(name)`: Grants the dynamic preset reference `(name)`.
- `/accounts capabilities action:revoke target:@user capabilities:(name)`: Revokes the dynamic preset reference `(name)`.
- `/accounts capabilities action:grant target:@user capabilities:server.(name)`: Grants the dynamic server-local preset reference `server.(name)`.
- `/accounts capabilities action:revoke target:@user capabilities:server.(name)`: Revokes the dynamic server-local preset reference `server.(name)`.
- `/accounts preset action:show name:name`: Shows the expanded capability list for a preset.
- `/accounts preset action:create name:name capabilities:a,b,c`: Creates or replaces a custom preset. Names use `\w+`, or `server.\w+` to define the server-specific version used by `server.(name)`.
- `/accounts preset action:remove name:name`: Removes a custom preset.
- Global presets: `default`, `user`, `ai`, `moderator`, `admin`.

Custom presets are stored globally in config under `account_capability_presets`. Preset capability entries can include `server.` or `.use` / `.grant`; these prefixes and suffixes are canonicalized when the preset resolves.

## Account

- `accounts.ban`: Ban or unban accounts from bot commands.
- `capabilities.manage`: Grant, revoke, reset, or resolve account capabilities.
- `capabilities.manage_presets`: Create, remove, or show account capability presets.

`/accounts ban` grants or revokes the `banned` capability globally or server-locally. The caller's effective `accounts.ban` depth must be greater than the target account's maximum effective permission depth in that scope. Global bans compare against the target's global and local depths because global `banned` overrides every server-local account view. Role bans are server-local only.

`banned`, `banned.use`, `banned.grant`, and server-local `server.banned` targets cannot be granted, revoked, reset away, or included in presets through generic capability management. Use `/accounts ban` for all ban and unban changes.

Account info and account listing are intentionally public through normal `commands` access for transparency.

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
