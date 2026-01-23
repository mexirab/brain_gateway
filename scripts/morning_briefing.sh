#!/bin/bash
# Morning Briefing Script for Brain Gateway
# Called by Home Assistant automation or cron
#
# Uses Jessica's cloned voice via Qwen3-TTS on Uranus
#
# Usage: ./morning_briefing.sh [speaker_entity]
# Example: ./morning_briefing.sh media_player.kitchen_display

SPEAKER="${1:-media_player.kitchen_display}"
ORCHESTRATOR_URL="http://localhost:8888"

echo "[$(date)] Starting morning briefing with Jessica's voice..."

# Generate briefing, TTS, and play on speaker - all in one call!
RESPONSE=$(curl -s -X POST "${ORCHESTRATOR_URL}/api/briefing/morning" \
  -H "Content-Type: application/json" \
  -d "{\"generate_tts\": true, \"play_on\": \"${SPEAKER}\"}")

BRIEFING=$(echo "$RESPONSE" | jq -r '.briefing')
VOICE=$(echo "$RESPONSE" | jq -r '.voice')
ANNOUNCED=$(echo "$RESPONSE" | jq -r '.announced_on')

if [ -z "$BRIEFING" ] || [ "$BRIEFING" == "null" ]; then
  echo "[$(date)] ERROR: Failed to generate briefing"
  echo "$RESPONSE"
  exit 1
fi

echo "[$(date)] Briefing: ${BRIEFING:0:100}..."
echo "[$(date)] Voice: ${VOICE}"
echo "[$(date)] Playing on: ${ANNOUNCED}"
echo "[$(date)] Morning briefing complete!"
