# Modlog Plan

## Original Proposal

> Oh and the mod logs are completely immutable except for by the bot host. And mod, admins, the server owner, and the bot host will be able to view the actions of moderators, former and current, even from before the bot came online and from mods that didn't use bogobot to perform their actions, as well as being able to undo those actions while restoring any configs that had to do with whatever you're restoring! Basically just making bogobot a really fucking good mod bot.

## What Can And Cannot Be Recorded

### Can Record Live While Bogobot Is Online

- Message deletes, including message content if Bogobot saw and cached the message before deletion.
- Bulk message deletes, with content only for messages Bogobot already saw and cached.
- Message edits, including before/after content if Bogobot saw the original message.
- Member bans and unbans.
- Member joins, leaves, and kicks. Kicks require audit-log correlation to distinguish from ordinary leaves.
- Member role changes.
- Member timeout changes.
- Member nickname and profile changes visible through guild member update events.
- Role creates, deletes, and updates.
- Channel creates, deletes, and updates.
- Thread creates, deletes, and updates.
- Bot-managed configuration changes when routed through Bogobot commands.
- Bogobot moderation commands and their outputs.

### Can Backfill From Discord Audit Logs

- Moderator actions performed before Bogobot came online, as far back as Discord audit logs are available.
- Actions performed by moderators who did not use Bogobot.
- Actions by former moderators, if Discord still exposes the actor id/user object in audit logs.
- Guild, channel, permission overwrite, member, role, invite, webhook, emoji, sticker, thread, automod, and onboarding/home settings changes listed in Discord's audit log action enum.
- Member role changes and timeout changes where audit log detail is available.
- Message moderation entries exposed by Discord audit logs, usually as metadata rather than full deleted message content.

### Cannot Record Or Recover Reliably

- Deleted message content from before Bogobot saw the message.
- Deleted message content from before Bogobot was online.
- Deleted message content if message content intent/cache policy did not capture it.
- Full context for all historical audit log actions; audit logs can omit old state needed for exact undo.
- Actions older than Discord's available audit log history.
- Moderator intent beyond the audit log reason field.
- Exact restoration of deleted messages. Bogobot can only repost a restoration copy if it has captured the content.
- Exact restoration of arbitrary pre-Bogobot config state unless Bogobot has a prior snapshot or the config change was logged by Bogobot.
- Host-proof immutability. The bot host can always change local files, database contents, backups, or code.

## Recording Matrix

Sources:

- `gateway`: live Discord events while Bogobot is online.
- `audit`: Discord audit log import/backfill.
- `bot`: Bogobot command/config hooks.
- `cache`: data Bogobot observed earlier, such as message content or previous object snapshots.

| Event / Action | gateway | audit | bot | cache | Historical backfill | Undo / restore notes |
| --- | --- | --- | --- | --- | --- | --- |
| `guild_update` | limited | yes | no | snapshot | yes, audit only | Restore known guild fields if old values are present. |
| `channel_create` | yes | yes | possible | snapshot | yes | Delete created channel if safe. |
| `channel_update` | yes | yes | possible | snapshot | yes | Restore known channel fields and overwrites if captured. |
| `channel_delete` | yes | yes | possible | snapshot | yes | Recreate approximately if snapshot exists; exact restoration is not guaranteed. |
| `overwrite_create` | via channel update | yes | possible | snapshot | yes | Remove created overwrite if still applicable. |
| `overwrite_update` | via channel update | yes | possible | snapshot | yes | Restore previous overwrite if captured. |
| `overwrite_delete` | via channel update | yes | possible | snapshot | yes | Recreate overwrite if previous value is known. |
| `kick` | member leave + audit correlation | yes | possible | no | yes | Cannot undo directly; user must rejoin or be invited. |
| `member_prune` | limited | yes | no | no | yes | Cannot undo; record affected count and reason. |
| `ban` | yes | yes | possible | no | yes | Undo by unbanning. |
| `unban` | yes | yes | possible | no | yes | Undo by re-banning if policy allows. |
| `member_update` | yes | yes | possible | snapshot | yes | Restore timeout/nickname/flags where Discord permits and old values are known. |
| `member_role_update` | yes | yes | possible | snapshot | yes | Restore role set or inverse role delta. |
| `member_move` | limited | yes | no | no | yes | Usually informational; can move back only if member is still in voice. |
| `member_disconnect` | limited | yes | no | no | yes | Informational; cannot undo. |
| `bot_add` | guild/member events | yes | no | no | yes | Usually informational; can kick bot if still present and authorized. |
| `role_create` | yes | yes | possible | snapshot | yes | Delete created role if safe. |
| `role_update` | yes | yes | possible | snapshot | yes | Restore known role fields. |
| `role_delete` | yes | yes | possible | snapshot | yes | Recreate approximately if snapshot exists; exact permissions/position may be imperfect. |
| `invite_create` | limited | yes | no | snapshot | yes | Delete invite if still active. |
| `invite_update` | limited | yes | no | snapshot | yes | Usually informational; restore may require recreating invite. |
| `invite_delete` | limited | yes | no | snapshot | yes | Recreate approximately if enough invite fields were captured. |
| `webhook_create` | limited | yes | no | snapshot | yes | Delete webhook if still present. |
| `webhook_update` | limited | yes | no | snapshot | yes | Restore known webhook fields if accessible. |
| `webhook_delete` | limited | yes | no | snapshot | yes | Recreate approximately only if enough fields were captured. |
| `emoji_create` | yes | yes | no | snapshot | yes | Delete emoji if still present. |
| `emoji_update` | yes | yes | no | snapshot | yes | Restore name if old value is known. |
| `emoji_delete` | yes | yes | no | snapshot | yes | Recreate only if image bytes were cached; otherwise log only. |
| `message_delete` | yes | yes | possible | message cache | yes, metadata only | Cannot undelete; repost restoration copy only if content was cached. |
| `message_bulk_delete` | yes | yes | possible | message cache | yes, metadata only | Cannot undelete; restore copies only for cached messages. |
| `message_pin` | yes | yes | possible | no | yes | Undo by unpinning if message still exists. |
| `message_unpin` | yes | yes | possible | no | yes | Undo by pinning if message still exists. |
| `integration_create` | limited | yes | no | snapshot | yes | Usually informational; restoration depends on integration type. |
| `integration_update` | limited | yes | no | snapshot | yes | Usually informational; restore if API exposes fields. |
| `integration_delete` | limited | yes | no | snapshot | yes | Usually cannot undo exactly. |
| `stage_instance_create` | yes | yes | no | snapshot | yes | Delete stage instance if still active. |
| `stage_instance_update` | yes | yes | no | snapshot | yes | Restore known fields if still active. |
| `stage_instance_delete` | yes | yes | no | snapshot | yes | Recreate approximately if enough fields were captured. |
| `sticker_create` | yes | yes | no | snapshot | yes | Delete sticker if still present. |
| `sticker_update` | yes | yes | no | snapshot | yes | Restore known sticker fields. |
| `sticker_delete` | yes | yes | no | snapshot | yes | Recreate only if asset bytes and fields were cached. |
| `scheduled_event_create` | yes | yes | no | snapshot | yes | Delete scheduled event if still present. |
| `scheduled_event_update` | yes | yes | no | snapshot | yes | Restore known scheduled event fields. |
| `scheduled_event_delete` | yes | yes | no | snapshot | yes | Recreate approximately if enough fields were captured. |
| `thread_create` | yes | yes | possible | snapshot | yes | Archive/delete thread if still present. |
| `thread_update` | yes | yes | possible | snapshot | yes | Restore known thread fields. |
| `thread_delete` | yes | yes | possible | snapshot | yes | Recreate approximately only if enough fields were captured. |
| `app_command_permission_update` | limited | yes | bot command hooks | snapshot | yes | Restore known command permission state if captured. |
| `soundboard_sound_create` | limited | yes | no | snapshot | yes | Delete sound if still present. |
| `soundboard_sound_update` | limited | yes | no | snapshot | yes | Restore known sound fields. |
| `soundboard_sound_delete` | limited | yes | no | snapshot | yes | Recreate only if asset bytes and fields were cached. |
| `automod_rule_create` | limited | yes | no | snapshot | yes | Delete rule if still present. |
| `automod_rule_update` | limited | yes | no | snapshot | yes | Restore known rule fields. |
| `automod_rule_delete` | limited | yes | no | snapshot | yes | Recreate approximately if rule snapshot exists. |
| `automod_block_message` | gateway message may be absent | yes | no | no | yes | Informational; blocked message may not be available. |
| `automod_flag_message` | limited | yes | no | no | yes | Informational. |
| `automod_timeout_member` | member update | yes | no | snapshot | yes | Restore timeout if old value is known and policy allows. |
| `automod_quarantine_user` | member/role update | yes | raid/verify hooks | snapshot | yes | Restore roles/quarantine state if captured. |
| `creator_monetization_request_created` | no | yes | no | no | yes | Informational. |
| `creator_monetization_terms_accepted` | no | yes | no | no | yes | Informational. |
| `onboarding_prompt_create` | limited | yes | no | snapshot | yes | Delete prompt if still present. |
| `onboarding_prompt_update` | limited | yes | no | snapshot | yes | Restore known prompt fields. |
| `onboarding_prompt_delete` | limited | yes | no | snapshot | yes | Recreate approximately if snapshot exists. |
| `onboarding_create` | limited | yes | no | snapshot | yes | Restore known onboarding state if captured. |
| `onboarding_update` | limited | yes | no | snapshot | yes | Restore known onboarding state if captured. |
| `home_settings_create` | limited | yes | no | snapshot | yes | Restore known home settings if captured. |
| `home_settings_update` | limited | yes | no | snapshot | yes | Restore known home settings if captured. |
| Message edit | yes | no | no | message cache | no | Restore previous content only by sending/editing a copy; original message can be edited only by its author/webhook. |
| Member join | yes | no | no | no | no | Informational; useful for raid/mod context. |
| Member leave without kick | yes | no | no | no | no | Informational; distinguish from kick by absence of matching audit entry. |
| Bogobot config change | no | no | yes | config snapshot | only if Bogobot logged it | Restore exact prior config when old value was logged. |
| Bogobot moderation command | command wrapper | maybe matching audit | yes | command output | only if Bogobot logged it | Undo through the command's modlog undo handler where available. |

## Goal

Build a durable moderation ledger for Bogobot that records moderator actions, imports best-effort historical Discord audit log data, exposes searchable views to authorized staff, and supports undo/restore flows where Discord exposes enough state to do so safely.

The system should be designed as a real moderation subsystem, not just a pretty log channel.

## Non-Goals And Reality Checks

- Bogobot cannot recover deleted message contents from before it was online.
- Bogobot can record deleted message contents only for messages it saw before deletion, assuming message content intent and cache/history collection allow it.
- Discord audit logs can provide many actions performed outside Bogobot, including former moderator actions, but audit logs are not infinite and do not contain every piece of reversible state.
- "Immutable" means immutable to Discord users and normal bot commands. The bot host can always edit local storage, backups, or source code.
- Undo is best effort. Some actions can be reversed exactly, some can be reversed approximately, and some can only be documented.

## Folder Shape

Proposed top-level module:

```text
modlog/
  PLAN.md
  __init__.py
  models.py
  store.py
  capture.py
  audit_import.py
  undo.py
  views.py
```

Discord command/plugin surface can still live in `plugins/modlog.py` and delegate into this package.

## Permissions

Suggested capabilities:

- `modlog.view`: view moderation ledger entries.
- `modlog.view_sensitive`: view sensitive payloads such as deleted message content.
- `modlog.import`: run audit-log import/backfill.
- `modlog.undo`: undo reversible moderation actions.
- `modlog.manage`: configure modlog behavior and retention.

Server-scoped forms should work:

- `server.modlog.view`
- `server.modlog.view_sensitive`
- `server.modlog.import`
- `server.modlog.undo`
- `server.modlog.manage`

## Storage

Use SQLite for the ledger. JSON files are fine for account/config style data, but moderation logs need indexed search, append-only-ish writes, and stable event IDs.

Suggested database:

```text
modlog.sqlite3
```

Core tables:

```text
events
  id integer primary key
  guild_id integer not null
  event_type text not null
  action text not null
  source text not null
  actor_id integer
  target_id integer
  channel_id integer
  message_id integer
  reason text
  created_at text not null
  observed_at text not null
  audit_log_id integer
  reversible integer not null
  sensitive integer not null
  payload_json text not null
  undo_payload_json text
  undone_by_event_id integer

event_hashes
  event_id integer primary key
  previous_hash text
  hash text not null

snapshots
  id integer primary key
  guild_id integer not null
  snapshot_type text not null
  object_id integer not null
  created_at text not null
  payload_json text not null
```

The hash chain is not true host-proof immutability, but it makes accidental mutation obvious and gives operators a tamper-evident ledger.

## Event Sources

### Live Bot Events

Capture events from Discord gateway callbacks:

- message delete
- bulk message delete
- message edit
- member ban/remove
- member unban
- member update
- guild role create/update/delete
- channel create/update/delete
- thread create/update/delete

For message deletion, store content only if Bogobot observed the message before deletion. If the content was not observed, store metadata and mark content as unavailable.

### Audit Log Import

Use `Guild.audit_logs` to backfill moderator actions:

- guild update
- channel create/update/delete
- permission overwrite create/update/delete
- kick
- member prune
- ban/unban
- member update
- member role update
- member move/disconnect
- bot add
- role create/update/delete
- invite create/update/delete
- webhook create/update/delete
- emoji create/update/delete
- message delete/bulk delete/pin/unpin
- integration create/update/delete
- stage instance create/update/delete
- sticker create/update/delete
- scheduled event create/update/delete
- thread create/update/delete
- app command permission update
- soundboard sound create/update/delete
- automod rule create/update/delete
- automod block/flag/timeout/quarantine actions
- creator monetization request/terms events
- onboarding prompt create/update/delete
- onboarding create/update
- home settings create/update

Import should deduplicate by audit log entry id.

Backfilled events should have:

```text
source = "audit_log_import"
observed_at = import time
created_at = audit log creation time
```

Live observed events that can be matched to audit logs should store both the gateway evidence and the audit log id.

Known Discord audit log actions:

```text
guild_update                                      = 1
channel_create                                    = 10
channel_update                                    = 11
channel_delete                                    = 12
overwrite_create                                  = 13
overwrite_update                                  = 14
overwrite_delete                                  = 15
kick                                              = 20
member_prune                                      = 21
ban                                               = 22
unban                                             = 23
member_update                                     = 24
member_role_update                                = 25
member_move                                       = 26
member_disconnect                                 = 27
bot_add                                           = 28
role_create                                       = 30
role_update                                       = 31
role_delete                                       = 32
invite_create                                    = 40
invite_update                                    = 41
invite_delete                                    = 42
webhook_create                                   = 50
webhook_update                                   = 51
webhook_delete                                   = 52
emoji_create                                     = 60
emoji_update                                     = 61
emoji_delete                                     = 62
message_delete                                   = 72
message_bulk_delete                              = 73
message_pin                                      = 74
message_unpin                                    = 75
integration_create                               = 80
integration_update                               = 81
integration_delete                               = 82
stage_instance_create                            = 83
stage_instance_update                            = 84
stage_instance_delete                            = 85
sticker_create                                   = 90
sticker_update                                   = 91
sticker_delete                                   = 92
scheduled_event_create                           = 100
scheduled_event_update                           = 101
scheduled_event_delete                           = 102
thread_create                                    = 110
thread_update                                    = 111
thread_delete                                    = 112
app_command_permission_update                    = 121
soundboard_sound_create                          = 130
soundboard_sound_update                          = 131
soundboard_sound_delete                          = 132
automod_rule_create                              = 140
automod_rule_update                              = 141
automod_rule_delete                              = 142
automod_block_message                            = 143
automod_flag_message                             = 144
automod_timeout_member                           = 145
automod_quarantine_user                          = 146
creator_monetization_request_created             = 150
creator_monetization_terms_accepted              = 151
onboarding_prompt_create                         = 163
onboarding_prompt_update                         = 164
onboarding_prompt_delete                         = 165
onboarding_create                                = 166
onboarding_update                                = 167
home_settings_create                             = 190
home_settings_update                             = 191
```

## Event Model

Each event should be normalized to a common shape:

```python
ModlogEvent(
    guild_id: int,
    event_type: str,
    action: str,
    source: str,
    actor_id: int | None,
    target_id: int | None,
    channel_id: int | None,
    message_id: int | None,
    reason: str | None,
    created_at: datetime,
    observed_at: datetime,
    audit_log_id: int | None,
    reversible: bool,
    sensitive: bool,
    payload: dict[str, object],
    undo_payload: dict[str, object] | None,
)
```

Examples:

- `member.ban`
- `member.unban`
- `member.kick`
- `member.timeout`
- `member.roles.add`
- `member.roles.remove`
- `message.delete`
- `message.bulk_delete`
- `channel.update`
- `role.update`

## Undo Strategy

Undo should be action-specific and explicit.

Examples:

- Ban: unban user.
- Unban: re-ban user if the actor has permission and payload has enough context.
- Timeout: restore previous timeout value.
- Role add/remove: restore previous role set or apply inverse role delta.
- Channel update: restore stored channel fields.
- Role update: restore stored role fields.
- Message delete: cannot truly undelete. If content was captured, repost as restoration copy and clearly mark it as restored by Bogobot.

Undo events should themselves be logged as new modlog events.

Never silently mutate history. A reverted action should point to the original event:

```text
undo_event.payload.original_event_id = ...
original_event.undone_by_event_id = undo_event.id
```

## Config Restoration

Some moderation actions affect bot config indirectly, especially:

- verification roles
- quarantine roles
- raid protection settings
- role capability assignments

For v1, handle these as explicit integrations:

- When raid/verify config changes, write a modlog event with old/new config values.
- Undo can restore the prior config value if the integration provides an undo handler.

Avoid trying to infer arbitrary config changes from Discord audit logs.

## User Interface

Commands:

```text
/modlog search
/modlog user
/modlog event
/modlog import
/modlog undo
/modlog config
```

Recommended first command set:

```text
/modlog user target:@user
/modlog event id:int
/modlog search action?:str actor?:user target?:user limit?:int
/modlog undo id:int
```

Use Components V2 card layout.

Sensitive fields should be hidden unless the viewer has `modlog.view_sensitive`.

## Import Flow

Audit import should be incremental:

1. Read last imported audit log id per guild.
2. Fetch newer entries.
3. Normalize supported entries.
4. Insert if not already present.
5. Store import cursor.

Manual backfill command can accept a time window or count limit.

## Integrity

Minimum integrity features:

- Append events rather than editing them.
- Use undo events instead of deleting events.
- Hash each event with previous hash.
- Store timestamps in UTC ISO format.
- Restrict deletion/compaction to bot host/manual maintenance only.

Optional later:

- Periodic signed ledger checkpoint.
- Export hash root to a Discord log channel.
- Read-only web UI.

## Implementation Phases

### Phase 1: Ledger Core

- Create `modlog/models.py`.
- Create SQLite store.
- Add append-only event insertion.
- Add search by guild, actor, target, action, time.
- Add hash chain.

### Phase 2: Live Capture

- Add gateway callbacks for message delete/edit and member/role/channel changes.
- Store reversible payloads where available.
- Add basic `/modlog user` and `/modlog event` views.

### Phase 3: Audit Import

- Import supported Discord audit log entries.
- Deduplicate by audit log id.
- Match live events to audit entries when possible.

### Phase 4: Undo

- Implement undo handlers for safe actions:
  - ban/unban
  - timeout
  - role add/remove
  - bot config changes
- Add `/modlog undo`.

### Phase 5: Sensitive Content

- Capture deleted message content for messages observed while online.
- Gate display behind `modlog.view_sensitive`.
- Add retention knobs.

### Phase 6: Polish

- Add pagination, filters, and export.
- Add scheduled audit-log sync.
- Add health/status panel.
- Add docs and capabilities list.
