#!/usr/bin/env bash
# Phase D soak review — checks all 10 metrics and emits GO/NO-GO per metric.
# Usage: bash scripts/soak_review.sh
# Requires: gcloud auth, project configured, API reachable.

set -uo pipefail

PROJECT="project-e4030c35-cf53-4c64-a78"
ZONE="us-central1-a"
VM="forensic-flight-prod-worker"
API_URL="https://forensic-flight-prod-api-xzidaowatq-uc.a.run.app"
REGION="us-central1"
SOAK_START="2026-05-16T10:03:00Z"
# Anthropic 400 credit errors that occurred before soak start
CREDIT_ERRORS_BASELINE=3

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
go()   { echo -e "  ${GREEN}[GO]${NC}    $*"; }
nogo() { echo -e "  ${RED}[NO-GO]${NC} $*"; FAILED=$((FAILED+1)); }
warn() { echo -e "  ${YELLOW}[WARN]${NC}  $*"; }

FAILED=0
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo "============================================================"
echo "  Phase D Soak Review"
echo "  Review time : $NOW"
echo "  Soak start  : $SOAK_START"
echo "============================================================"
echo ""

# ── SSH batch 1: container inspect (fast, no redis exec) ─────────
SSH1=$(gcloud compute ssh "$VM" \
  --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap \
  --command='
echo "PARSE_RESTART=$(sudo docker inspect forensic-flight-worker-parse-1 --format "{{.RestartCount}}" 2>/dev/null || echo -1)"
echo "INV_RESTART=$(sudo docker inspect forensic-flight-worker-investigate-1 --format "{{.RestartCount}}" 2>/dev/null || echo -1)"
echo "PARSE_STATUS=$(sudo docker inspect forensic-flight-worker-parse-1 --format "{{.State.Status}}" 2>/dev/null || echo unknown)"
echo "INV_STATUS=$(sudo docker inspect forensic-flight-worker-investigate-1 --format "{{.State.Status}}" 2>/dev/null || echo unknown)"
echo "REDIS_STATUS=$(sudo docker inspect forensic-flight-redis-1 --format "{{.State.Status}}" 2>/dev/null || echo unknown)"
echo "QDRANT_STATUS=$(sudo docker inspect forensic-flight-qdrant-1 --format "{{.State.Status}}" 2>/dev/null || echo unknown)"
' 2>/dev/null | grep "^[A-Z_]*=")

# ── SSH batch 2: memory, redis, logs ─────────────────────────────
SSH2=$(gcloud compute ssh "$VM" \
  --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap \
  --command='
REDIS_PASS=$(sudo grep "^REDIS_PASSWORD" /opt/forensic-flight/.env 2>/dev/null | cut -d= -f2)
MEM_AVAIL=$(awk "/MemAvailable/{print int(\$2/1024)}" /proc/meminfo)
REDIS_USED=$(sudo docker exec forensic-flight-redis-1 redis-cli -a "$REDIS_PASS" INFO memory 2>/dev/null | awk -F: "/^used_memory:/{print int(\$2/1048576)}")
REDIS_EVICTED=$(sudo docker exec forensic-flight-redis-1 redis-cli -a "$REDIS_PASS" INFO stats 2>/dev/null | awk -F: "/^evicted_keys:/{print \$2}" | tr -d "\r\n")
OOM=$(sudo dmesg 2>/dev/null | grep -c "oom-killer" || true)
C400=$(sudo docker logs forensic-flight-worker-investigate-1 2>&1 | grep -c "credit balance" || true)
PTOUT=$(sudo docker logs forensic-flight-worker-parse-1 2>&1 | grep -c "SoftTimeLimitExceeded" || true)
ITOUT=$(sudo docker logs forensic-flight-worker-investigate-1 2>&1 | grep -c "SoftTimeLimitExceeded" || true)
QSIZE=$(sudo du -sm /mnt/qdrant-data/ 2>/dev/null | cut -f1)
printf "MEM_AVAIL_MB=%s\nOOM_COUNT=%s\nREDIS_USED_MB=%s\nREDIS_EVICTED=%s\nCREDIT_ERRORS=%s\nPARSE_TIMEOUTS=%s\nINV_TIMEOUTS=%s\nQDRANT_SIZE_MB=%s\n" \
  "$MEM_AVAIL" "$OOM" "${REDIS_USED:-0}" "${REDIS_EVICTED:-0}" "$C400" "$PTOUT" "$ITOUT" "${QSIZE:-0}"
' 2>/dev/null | grep "^[A-Z_]*=")

eval "$SSH1" 2>/dev/null || true
eval "$SSH2" 2>/dev/null || true

PARSE_RESTART=${PARSE_RESTART:--1}; INV_RESTART=${INV_RESTART:--1}
PARSE_STATUS=${PARSE_STATUS:-unknown}; INV_STATUS=${INV_STATUS:-unknown}
REDIS_STATUS=${REDIS_STATUS:-unknown}; QDRANT_STATUS=${QDRANT_STATUS:-unknown}
MEM_AVAIL_MB=${MEM_AVAIL_MB:-0}; OOM_COUNT=${OOM_COUNT:-0}
REDIS_USED_MB=${REDIS_USED_MB:--1}; REDIS_EVICTED=${REDIS_EVICTED:-0}
CREDIT_ERRORS=${CREDIT_ERRORS:-0}
PARSE_TIMEOUTS=${PARSE_TIMEOUTS:-0}; INV_TIMEOUTS=${INV_TIMEOUTS:-0}
QDRANT_SIZE_MB=${QDRANT_SIZE_MB:-0}

# ── M1: Worker uptime / restart count ───────────────────────────
echo "[M1] Worker restart counts"
TOTAL_RESTARTS=$(( PARSE_RESTART + INV_RESTART ))
if [ "$PARSE_RESTART" -lt 0 ] || [ "$INV_RESTART" -lt 0 ]; then
  nogo "Could not read restart counts — container missing or SSH failed"
elif [ "$TOTAL_RESTARTS" -le 2 ]; then
  go "parse=$PARSE_RESTART  investigate=$INV_RESTART  total=$TOTAL_RESTARTS (threshold ≤2)"
else
  nogo "parse=$PARSE_RESTART  investigate=$INV_RESTART  total=$TOTAL_RESTARTS (threshold ≤2)"
fi
echo ""

# ── M2: Container status ─────────────────────────────────────────
echo "[M2] Container status"
for pair in "worker-parse:$PARSE_STATUS" "worker-investigate:$INV_STATUS" "redis:$REDIS_STATUS" "qdrant:$QDRANT_STATUS"; do
  NAME="${pair%%:*}"; STATUS="${pair##*:}"
  [ "$STATUS" = "running" ] && go "$NAME → $STATUS" || nogo "$NAME → $STATUS (expected: running)"
done
echo ""

# ── M3: VM memory ────────────────────────────────────────────────
echo "[M3] VM memory"
if [ "$OOM_COUNT" -gt 0 ]; then
  nogo "OOM killer fired $OOM_COUNT time(s) — check dmesg"
elif [ "$MEM_AVAIL_MB" -ge 500 ]; then
  go "${MEM_AVAIL_MB} MB available (threshold ≥500 MB)"
else
  nogo "${MEM_AVAIL_MB} MB available (threshold ≥500 MB)"
fi
echo ""

# ── M4: Redis memory ─────────────────────────────────────────────
echo "[M4] Redis memory"
if [ "$REDIS_USED_MB" -lt 0 ]; then
  warn "Could not read Redis memory — check redis container"
elif [ "$REDIS_EVICTED" -gt 0 ]; then
  nogo "${REDIS_USED_MB} MB used — ${REDIS_EVICTED} keys evicted (data loss risk)"
elif [ "$REDIS_USED_MB" -le 450 ]; then
  go "${REDIS_USED_MB} MB used, 0 evictions (threshold ≤450 MB)"
else
  nogo "${REDIS_USED_MB} MB used — approaching 512 MB cap"
fi
echo ""

# ── M5: Anthropic credit failures post-soak ──────────────────────
echo "[M5] Anthropic credit failures (post soak-start)"
POST_SOAK_CREDIT=$(( CREDIT_ERRORS - CREDIT_ERRORS_BASELINE ))
[ "$POST_SOAK_CREDIT" -lt 0 ] && POST_SOAK_CREDIT=0
if [ "$POST_SOAK_CREDIT" -eq 0 ]; then
  go "0 credit-balance 400s since soak start (${CREDIT_ERRORS} total, ${CREDIT_ERRORS_BASELINE} pre-soak)"
else
  nogo "${POST_SOAK_CREDIT} credit-balance 400s since soak start — add Anthropic credits"
fi
echo ""

# ── M6/M7: Cloud Run 5xx + service health ────────────────────────
echo "[M6/M7] API 5xx count and service status"
FIVE_XX=$(gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=forensic-flight-prod-api AND httpRequest.status>=500 AND timestamp>=\"$SOAK_START\"" \
  --project="$PROJECT" --limit=20 \
  --format="value(httpRequest.status)" 2>/dev/null | wc -l | tr -d ' ')
CR_STATUS=$(gcloud run services describe forensic-flight-prod-api \
  --region="$REGION" --project="$PROJECT" \
  --format="value(status.conditions[0].status)" 2>/dev/null)
[ "${FIVE_XX:-0}" -le 2 ] && go "${FIVE_XX:-0} 5xx since soak start (threshold ≤2)" || nogo "${FIVE_XX} 5xx since soak start (threshold ≤2)"
[ "$CR_STATUS" = "True" ] && go "Cloud Run revision healthy" || nogo "Cloud Run not ready: $CR_STATUS"
echo ""

# ── M8: Successful investigations ────────────────────────────────
echo "[M8] Flights with status=ready"
READY_FLIGHTS=$(curl -sL --max-time 10 "$API_URL/api/v1/flights/" 2>/dev/null | \
  python3 -c "
import json, sys
try:
    flights = json.loads(sys.stdin.read())
    print(len([f for f in flights if f.get('status') == 'ready']))
except:
    print(-1)
" 2>/dev/null)
if [ "${READY_FLIGHTS:--1}" -lt 0 ]; then
  warn "Could not reach API — check Cloud Run"
elif [ "${READY_FLIGHTS}" -ge 1 ]; then
  go "${READY_FLIGHTS} flight(s) with status=ready"
else
  nogo "0 flights in ready state — no successful end-to-end investigations"
fi
echo ""

# ── M9: Celery timeout events ────────────────────────────────────
echo "[M9] Celery soft/hard timeout events"
TOTAL_TIMEOUTS=$(( PARSE_TIMEOUTS + INV_TIMEOUTS ))
if [ "$TOTAL_TIMEOUTS" -eq 0 ]; then
  go "0 timeout events (parse=$PARSE_TIMEOUTS investigate=$INV_TIMEOUTS)"
else
  nogo "${TOTAL_TIMEOUTS} timeout event(s): parse=$PARSE_TIMEOUTS investigate=$INV_TIMEOUTS"
fi
echo ""

# ── M10: Qdrant persistence ──────────────────────────────────────
echo "[M10] Qdrant persistence"
if [ "$QDRANT_STATUS" != "running" ]; then
  nogo "Qdrant container is $QDRANT_STATUS (expected: running)"
elif [ "${QDRANT_SIZE_MB:-0}" -eq 0 ]; then
  nogo "Qdrant data dir is 0 MB — persistent disk may be unmounted"
else
  go "Qdrant running, ${QDRANT_SIZE_MB} MB on /mnt/qdrant-data (persistent disk)"
fi
echo ""

# ── Final verdict ─────────────────────────────────────────────────
echo "============================================================"
if [ "$FAILED" -eq 0 ]; then
  echo -e "  ${GREEN}OVERALL: GO${NC} — all 10 metrics passed"
  echo "  Proceed to AWS teardown runbook."
else
  echo -e "  ${RED}OVERALL: NO-GO${NC} — $FAILED metric(s) failed"
  echo "  Resolve all failures before re-evaluating teardown."
fi
echo "============================================================"
exit "$FAILED"
