# Local setup (MODE=polling)

Step-by-step guide to run `telegram-excerpt` locally via Docker Compose
using long polling (no public webhook required).

## 1. Prerequisites

- **Docker** + Docker Compose
- A **Google Cloud** project with **Firestore Native** enabled
- An **OpenRouter** account with an API key
- At least 2 Telegram bots created via
  [@BotFather](https://t.me/BotFather):
  - one **admin** bot (your control panel)
  - one or more **child** bots (one per group to monitor)

## 2. Firestore service account

```bash
# In your GCP project:
gcloud iam service-accounts create telegram-excerpt \
    --display-name="Telegram Excerpt"

gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:telegram-excerpt@$PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/datastore.user"

gcloud iam service-accounts keys create ./secrets/firebase.json \
    --iam-account="telegram-excerpt@$PROJECT_ID.iam.gserviceaccount.com"
```

The `./secrets/firebase.json` file is mounted into the container at
runtime. **Do not commit it**.

## 3. `.env` file

```bash
cp .env.example .env
```

Edit `.env` with your real values. For polling mode the minimum set is:

```
TELEGRAM_ADMIN_BOT_TOKEN=<your-admin-token>
FORWARD_CHAT_ID=<your-user-id>
OPENROUTER_API_KEY=<your-key>
GOOGLE_APPLICATION_CREDENTIALS=/app/secrets/firebase.json
FIRESTORE_PROJECT_ID=<your-gcp-project>
MODE=polling
```

### How to find your `FORWARD_CHAT_ID`

1. Write any message to your admin bot in private chat.
2. Open in a browser: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Look for the `message.chat.id` field — it's a positive integer
   (e.g. `123456789`).

## 4. Start

```bash
docker-compose up --build
```

On first boot the console shows:

```
main.start mode=polling
manager.reloaded count=0
main.polling.started scheduler_interval=30
```

## 5. Registering a child bot

### 5a. Prepare the child bot

In the chat with [@BotFather](https://t.me/BotFather):

1. `/newbot` → follow the prompts, get the token.
2. **Important:** `/setprivacy` → select the bot → **Disable**.
   Without this, the bot will only receive messages that mention it.
3. Add the bot to the target group as **admin** (or as a regular member,
   as long as privacy mode is disabled).

### 5b. Find the group `chat_id`

Fastest way: add the child bot to the group, send any message, then
visit:

```
https://api.telegram.org/bot<CHILD_BOT_TOKEN>/getUpdates
```

Look for `message.chat.id` — for groups it's a **negative** number
(e.g. `-1001234567890`).

### 5c. Register it via the admin bot

In private chat with the admin bot:

```
/add_bot <CHILD_BOT_TOKEN> -1001234567890 20
```

(`N=20` means max 20 messages per batch; omit to use the default).

Expected reply:

```
✅ Bot registrato su Your group name
chat_id: -1001234567890
N: 20
```

## 6. End-to-end verification

1. Write 3-4 messages in the monitored group.
2. Check the console: `manager.message.buffered` per message.
3. Wait 3 minutes of silence (or lower `BATCH_SILENCE_SECONDS` in
   `.env` for faster tests — minimum is `30`).
4. You should see in the logs: `processor.flush.start` →
   `llm.classify.done` → `processor.flush.done`.
5. If the conversation was "actionable", N `.md` files arrive in your
   private chat from the admin bot.

## Troubleshooting

**The child bot does not receive messages.**
Privacy mode is not disabled. Go to @BotFather → `/setprivacy` →
Disable, then **remove and re-add** the bot to the group (the change
only applies to new sessions).

**`manager.handler.unexpected_chat`.**
The child bot was added to a group other than the registered one.
Remove it from the other groups.

**`storage failed: ... indexes ...`.**
Firestore requires a composite index for `list_silent_bots`. On first
error Firestore prints a link in the message: open it, click "Create
index", wait ~1 min.

**`OPENROUTER_API_KEY` invalid.**
Make sure you have a payment method configured on OpenRouter even for
`:free` models — OpenRouter requires billing set up (but charges
nothing for free-tier models).
