# Incoming message logs

Incoming private and group messages received by the bot are logged to
`LOG_CHANNEL`, including searches, commands, text, captions and media.
Each message has a sender-name/ID/username and source-chat header; a copy of
the original message is posted as a reply to that header. Anonymous group
senders are identified by their sender chat where available.

## Setup

- Set `LOG_CHANNEL` to your private log channel's numeric ID (`-100…`).
- Add the bot as a channel admin with permission to post.
- `MESSAGE_LOGGING=True` is the default. Set it to `False` and restart to stop.
- Notify users that messages sent to the bot are logged for administration.
  Restrict log-channel access and remove old logs when no longer needed.

Logging runs before maintenance/search/conversation handlers and does not
consume messages. Logging failures are warnings, with a 10-second timeout,
so a missing permission or protected message does not prevent normal handling.
Messages in the log channel, service events and bot senders are excluded.
The old PM-disabled-only message log was removed to avoid duplicate entries.

Telegram limitations: the bot cannot see unsent text, other people's private
chats, or group messages Telegram does not deliver to it. To receive ordinary
group messages, configure BotFather privacy mode appropriately (or make the
bot a group admin). Protected content and unsupported message types may not
be copyable; in that case only the header may be posted. Button clicks,
inline queries, edits and historical messages are not included.
