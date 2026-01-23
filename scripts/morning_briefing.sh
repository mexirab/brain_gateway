#!/bin/bash
# Morning Briefing Script for Brain Gateway
# Called by Home Assistant automation or cron
#
# Usage: ./morning_briefing.sh [speaker_entity]
# Example: ./morning_briefing.sh media_player.kitchen_display

SPEAKER="${1:-media_player.kitchen_display}"
ORCHESTRATOR_URL="http://localhost:8888"
TTS_URL="http://10.0.0.173:8002"
HA_URL="http://10.0.0.106:8123"
HA_TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI5YjcwZmQ0YWJmMTY0OWEyOTQ4YjIzYTQ4YjQ1NDlhNCIsImlhdCI6MTc2Nzg5MzE0MSwiZXhwIjoyMDgzMjUzMTQxfQ.7n2TOJrEH1hMGM1e-vlEatj9bnGg13qmcihXU0nbI7o"

echo "[$(date)] Starting morning briefing..."

# Generate briefing and TTS
RESPONSE=$(curl -s -X POST "${ORCHESTRATOR_URL}/api/briefing/morning" \
  -H "Content-Type: application/json" \
  -d '{"generate_tts": true}')

BRIEFING=$(echo "$RESPONSE" | jq -r '.briefing')
AUDIO_FILE=$(echo "$RESPONSE" | jq -r '.audio_file')

if [ -z "$BRIEFING" ] || [ "$BRIEFING" == "null" ]; then
  echo "[$(date)] ERROR: Failed to generate briefing"
  exit 1
fi

echo "[$(date)] Briefing generated: ${BRIEFING:0:100}..."

# Use HA's TTS service to announce on speaker
curl -s -X POST "${HA_URL}/api/services/tts/speak" \
  -H "Authorization: Bearer ${HA_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"entity_id\": \"tts.google_translate_en_com\",
    \"media_player_entity_id\": \"${SPEAKER}\",
    \"message\": $(echo "$BRIEFING" | jq -Rs .)
  }"

echo "[$(date)] Briefing sent to ${SPEAKER}"

# Clean up temp audio file
if [ -f "$AUDIO_FILE" ]; then
  rm -f "$AUDIO_FILE"
fi

echo "[$(date)] Morning briefing complete!"
