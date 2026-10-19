#!/usr/bin/env bash
# End-to-end demo against a running stack (`docker compose up`). Needs curl and jq.
#   ./examples/demo.sh path/to/file.jpg image/jpeg
set -euo pipefail

API="${API:-http://localhost:8000}"
ADMIN_TOKEN="${ADMIN_TOKEN:-dev-admin-token}"
FILE="${1:?usage: demo.sh <file> <content-type>}"
TYPE="${2:?usage: demo.sh <file> <content-type>}"

# 1. Create a project (one-off). The API key is only shown once.
KEY=$(curl -fsS -X POST "$API/v1/admin/projects" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Demo app"}' | jq -r .api_key)
AUTH="Authorization: Bearer $KEY"

# 2. Ask for an upload URL.
SIZE=$(wc -c < "$FILE" | tr -d ' ')
UPLOAD=$(curl -fsS -X POST "$API/v1/uploads" -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"filename\":\"$(basename "$FILE")\",\"content_type\":\"$TYPE\",\"size\":$SIZE}")
FILE_ID=$(jq -r .file_id <<<"$UPLOAD")
URL=$(jq -r .upload.url <<<"$UPLOAD")

# 3. Upload straight to storage (the API never sees the bytes).
curl -fsS -X PUT "$URL" -H "Content-Type: $TYPE" --data-binary "@$FILE"

# 4. Tell MediaPipe the upload is done; processing starts.
curl -fsS -X POST "$API/v1/uploads/$FILE_ID/complete" -H "$AUTH" | jq '{status: .file.status, jobs: .jobs}'

# 5. Wait for the result (or stream it: curl -N "$API/v1/files/$FILE_ID/events" -H "$AUTH").
until [[ "$(curl -fsS "$API/v1/files/$FILE_ID" -H "$AUTH" | jq -r .status)" =~ ^(processed|failed)$ ]]; do
  sleep 1
done
curl -fsS "$API/v1/files/$FILE_ID" -H "$AUTH" | jq '{status, error, metadata, variants: [.variants[] | {name, size, url}]}'
