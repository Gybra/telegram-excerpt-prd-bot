# Deploy on Cloud Run (MODE=webhook)

Guide to deploy `telegram-excerpt` on **Google Cloud Run** while staying
inside the **free tier**. Requires:

- A GCP account with billing enabled (the free tier applies even with
  billing on).
- `gcloud` CLI authenticated (`gcloud auth login`).

## 1. GCP project setup

```bash
export PROJECT_ID=my-telegram-excerpt
export REGION=europe-west1

gcloud projects create $PROJECT_ID
gcloud config set project $PROJECT_ID
gcloud services enable \
    run.googleapis.com \
    firestore.googleapis.com \
    cloudscheduler.googleapis.com \
    secretmanager.googleapis.com \
    artifactregistry.googleapis.com
```

## 2. Firestore

```bash
gcloud firestore databases create --region=$REGION --type=firestore-native
```

## 3. Service account + secrets

```bash
# Service account for the app (Cloud Run runtime)
gcloud iam service-accounts create telegram-excerpt-app \
    --display-name="Telegram Excerpt Runtime"

gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:telegram-excerpt-app@$PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/datastore.user"

# Generate the service-account JSON and upload it as a secret
gcloud iam service-accounts keys create firebase.json \
    --iam-account="telegram-excerpt-app@$PROJECT_ID.iam.gserviceaccount.com"

gcloud secrets create firebase-json --data-file=firebase.json
rm firebase.json  # don't keep it locally

# Secret for scheduler auth token
openssl rand -hex 32 | gcloud secrets create scheduler-auth-token --data-file=-

# Secrets for OpenRouter key and admin bot token
echo -n "<your-openrouter-key>" | gcloud secrets create openrouter-key --data-file=-
echo -n "<your-admin-bot-token>" | gcloud secrets create telegram-admin-token --data-file=-
```

Grant the runtime permission to read the secrets:

```bash
for SECRET in firebase-json scheduler-auth-token openrouter-key telegram-admin-token; do
  gcloud secrets add-iam-policy-binding $SECRET \
    --member="serviceAccount:telegram-excerpt-app@$PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

## 4. Build & deploy

```bash
# Build image via Cloud Build + push to Artifact Registry
gcloud artifacts repositories create apps --repository-format=docker --location=$REGION
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/apps/telegram-excerpt:latest"
gcloud builds submit --tag=$IMAGE

# Deploy — first deploy without BASE_URL
gcloud run deploy telegram-excerpt \
    --image=$IMAGE \
    --region=$REGION \
    --service-account=telegram-excerpt-app@$PROJECT_ID.iam.gserviceaccount.com \
    --no-allow-unauthenticated \
    --memory=512Mi \
    --cpu=1 \
    --min-instances=0 \
    --max-instances=3 \
    --timeout=120 \
    --set-env-vars="MODE=webhook,FIRESTORE_PROJECT_ID=$PROJECT_ID,GOOGLE_APPLICATION_CREDENTIALS=/secrets/firebase.json,FORWARD_CHAT_ID=<your-user-id>,OPENROUTER_MODEL=qwen/qwen3.6-plus:free,BATCH_SILENCE_SECONDS=180,DEFAULT_N=50" \
    --set-secrets="TELEGRAM_ADMIN_BOT_TOKEN=telegram-admin-token:latest,OPENROUTER_API_KEY=openrouter-key:latest,SCHEDULER_AUTH_TOKEN=scheduler-auth-token:latest,/secrets/firebase.json=firebase-json:latest"
```

> **Note on public webhook**: Telegram must be able to reach
> `/webhook/{hash}` **without GCP authentication**. Two options:
>
> 1. **Allow unauthenticated** (`--allow-unauthenticated`). The
>    endpoints remain protected by
>    `X-Telegram-Bot-Api-Secret-Token` + bearer for `/tasks/*` and
>    `/admin/*`.
> 2. Put a reverse proxy / Cloud Endpoints in front. More complex.
>
> For a personal self-host, (1) is acceptable. If you go with (1):
> replace `--no-allow-unauthenticated` with `--allow-unauthenticated`.

Get the URL:

```bash
BASE_URL=$(gcloud run services describe telegram-excerpt --region=$REGION --format='value(status.url)')
echo $BASE_URL
```

Update the `BASE_URL` env var and redeploy:

```bash
gcloud run services update telegram-excerpt \
    --region=$REGION \
    --update-env-vars="BASE_URL=$BASE_URL"
```

## 5. Cloud Scheduler

If you used `--allow-unauthenticated` in step 4, the scheduler only
needs the application-level bearer token:

```bash
SCHEDULER_TOKEN=$(gcloud secrets versions access latest --secret=scheduler-auth-token)

gcloud scheduler jobs create http telegram-excerpt-flush \
    --location=$REGION \
    --schedule="* * * * *" \
    --uri="$BASE_URL/tasks/process" \
    --http-method=POST \
    --headers="Authorization=Bearer $SCHEDULER_TOKEN" \
    --attempt-deadline=60s
```

If instead you kept `--no-allow-unauthenticated`, the scheduler must
also present an OIDC token so Cloud Run lets the request through.
Grant the invoker role and add `--oidc-service-account-email`:

```bash
# Grant Cloud Scheduler permission to invoke the service
gcloud run services add-iam-policy-binding telegram-excerpt \
    --region=$REGION \
    --member="serviceAccount:telegram-excerpt-app@$PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/run.invoker"

SCHEDULER_TOKEN=$(gcloud secrets versions access latest --secret=scheduler-auth-token)

gcloud scheduler jobs create http telegram-excerpt-flush \
    --location=$REGION \
    --schedule="* * * * *" \
    --uri="$BASE_URL/tasks/process" \
    --http-method=POST \
    --headers="Authorization=Bearer $SCHEDULER_TOKEN" \
    --oidc-service-account-email="telegram-excerpt-app@$PROJECT_ID.iam.gserviceaccount.com" \
    --attempt-deadline=60s
```

## 6. Setup webhooks

```bash
curl -X POST "$BASE_URL/admin/setup" \
    -H "Authorization: Bearer $SCHEDULER_TOKEN"
```

Expected response (after you register at least one bot via `/add_bot`):

```json
{"admin":"ok","bots":[{"chat_id":-100123,"status":"ok"}]}
```

## 7. Verification

- Health: `curl $BASE_URL/health` → `{"status":"ok"}`
- Message your admin bot in private chat: `/help`
- Register a child bot: `/add_bot <token> <chat_id> 20`
- Send messages in the group, wait 3 min, receive the PRDs.

## Free-tier quota monitoring

```bash
# Cloud Run requests / vCPU
gcloud monitoring dashboards list

# Firestore usage
gcloud firestore operations list
```

GCP Console → Billing → Reports filtered by "Free tier usage" shows
real-time consumption.

## Cleanup

```bash
gcloud run services delete telegram-excerpt --region=$REGION
gcloud scheduler jobs delete telegram-excerpt-flush --location=$REGION
gcloud firestore databases delete --database='(default)' --location=$REGION  # CAREFUL
```
