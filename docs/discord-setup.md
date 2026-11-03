# Discord setup

Do this once per server. warden needs its own bot. Do not reuse another bot's token: whoever
holds this one can ban.

## 1. The application

In the [Developer Portal](https://discord.com/developers/applications):

1. **New Application**. Name it, e.g. "Enth Warden".
2. Under **Bot**, then **Privileged Gateway Intents**:
   - **Server Members Intent: on.** warden needs it for role changes, joins and the startup sweep.
   - **Message Content Intent: off.** A ban needs the author, never the text.
3. Under **Bot**, use **Reset Token** and copy it **straight into the token file**, never into chat:
   ```sh
   install -m 600 /dev/null ~/.warden/token && open -e ~/.warden/token
   ```
   In TextEdit use **Format → Make Plain Text** before pasting, then save. If saving loosens
   the file's mode, `chmod 600 ~/.warden/token`.

## 2. Invite it with the least it needs

Permission integer **93188**:

| Permission | Why |
|---|---|
| Ban Members | the bans |
| Manage Messages | deleting honeypot posts |
| View Channels | Discord only delivers a channel's messages to bots that can see it |
| Read Message History | catching up on the honeypot after a restart |
| Send Messages, Embed Links | reports |

The invite URL. Replace `APP_ID` and `GUILD_ID`; the last two parameters lock it to that server:

```
https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot&permissions=93188&guild_id=GUILD_ID&disable_guild_select=true
```

## 3. Server settings

- **Role order** (Server Settings → Roles): put warden's role **above** the banned role and
  every role a target might hold, and **below** staff. Discord refuses a ban against anyone at
  or above the bot.
- **Channel overrides**, if the channels are private. Add warden's role and allow:
  - **report channel:** View Channel, Send Messages, Embed Links;
  - **honeypot channel:** View Channel, Read Message History, Manage Messages.
- **Mark the honeypot:** a topic and a pinned message saying anyone who posts there is banned.
  Staff are exempt, so they can post that.

## 4. Ids for the config

Turn on **User Settings → Advanced → Developer Mode**, then right-click and **Copy ID**:

- the server;
- the role to ban;
- the honeypot channel;
- the report channel;
- any staff roles.

Put them in `~/.warden/warden.toml`; `warden.toml.example` documents every key.

On every start, warden lists any setup problem it can see in the report channel: a missing
permission, a hidden channel, or its role ranking too low. Fix those first.
