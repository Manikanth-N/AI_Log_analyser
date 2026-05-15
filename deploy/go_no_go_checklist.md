# Forensic Flight AI — Production Go/No-Go Checklist

## How to use this checklist

Run through every item before executing `terraform apply` against production.
Mark each item ✅ PASS, ❌ FAIL, or ⚠️ WAIVED (with written justification).
A single ❌ is a hard block. A WAIVED item requires a decision log entry.

---

## 1. Benchmark gate

**Required before any deployment. Zero exceptions.**

| Check | Command | Pass Condition |
|---|---|---|
| Unit suite passes | `pytest tests/unit/ -q` | All pass, 0 failures |
| Benchmark stub suite passes | `pytest tests/benchmark/ -q` | 28 pass, 12 skip (expected) |
| Classification accuracy | Run bat_anomaly_002 live | ANOMALY (not CRASH) |
| Inverse guard | Run bat_anomaly_004 live | ANOMALY, no GPS/EKF factors |
| GPS crash grounding | Run gps_crash_006 live | CRASH/HIGH, GPS/EKF evidence cited |
| **Narrative safety gate** | `pytest tests/unit/test_narrative_validator.py` | All 21 pass |
| Cross-domain hallucination | bat_anomaly_002 executive_summary | No GPS/EKF keywords |

**Acceptance gate:** >90% classification accuracy, zero fabricated contributing factors, zero cross-domain hallucinations in narrative fields.

**Current status:** Unit gate: ✅ 146 passed. Live benchmark: requires Ollama/LLM to run.

---

## 2. Security gate

### API protection
- [ ] `API_KEYS` env var is set with at least one strong key (>32 chars)
- [ ] `ENABLE_DOCS=false` set in production env (hides Swagger UI)
- [ ] Rate limits configured: `RATE_LIMIT_INVESTIGATIONS_PER_DAY=50`, `RATE_LIMIT_UPLOADS_PER_HOUR=100`
- [ ] Upload validation tested: `.exe` upload returns 415
- [ ] Upload validation tested: oversized file returns 413
- [ ] Upload validation tested: filename with `../` traversal returns 400

### Network isolation
- [ ] API ECS service has no public IP
- [ ] Workers have no inbound security group rules
- [ ] RDS is not publicly accessible (`publicly_accessible = false` in Terraform)
- [ ] ElastiCache is not accessible from the internet
- [ ] NAT Gateway in place for outbound traffic
- [ ] ALB security group: only 80/443 from 0.0.0.0/0

### Secrets
- [ ] No API keys in environment variables in `.env` on developer machines
- [ ] Anthropic key stored in Secrets Manager: `forensic-flight-prod/anthropic-api-key`
- [ ] OpenAI key stored in Secrets Manager: `forensic-flight-prod/openai-api-key`
- [ ] DB password in Secrets Manager (not in task definition plaintext)
- [ ] ECS execution role has secretsmanager:GetSecretValue for exactly these 5 secrets (not `*`)
- [ ] GitHub Actions uses OIDC (no long-lived AWS access keys in GitHub secrets)

### TLS
- [ ] ACM certificate issued and validated for `api.yourdomain.com`
- [ ] ALB HTTPS listener configured with `ELBSecurityPolicy-TLS13-1-2-2021-06`
- [ ] HTTP listener redirects to HTTPS (301)
- [ ] CloudFront: `redirect-to-https` viewer protocol policy

### Validate with:
```bash
# Test that HTTP redirects to HTTPS
curl -v http://api.yourdomain.com/health 2>&1 | grep "Location:"

# Test that API key auth is enforced
curl -X POST https://api.yourdomain.com/api/v1/investigations \
  -H "Content-Type: application/json" \
  -d '{"flight_id": "test"}'
# Expected: 401 or 403

# Test that docs are hidden
curl -s -o /dev/null -w "%{http_code}" https://api.yourdomain.com/docs
# Expected: 404
```

---

## 3. Cost controls gate

- [ ] AWS Budget alarm created: `forensic-flight-prod-monthly-budget`
- [ ] Budget threshold set to 2× expected monthly spend
- [ ] Alert email confirmed (SNS subscription approved in inbox)
- [ ] `RATE_LIMIT_INVESTIGATIONS_PER_DAY=50` set per API key
- [ ] NAT Gateway bytes alarm active (`forensic-flight-prod-nat-bytes-high`)
- [ ] Token usage logging confirmed: `InferenceClient.log_usage_summary()` appears in CloudWatch logs after a test investigation
- [ ] Verify cost estimate: expected < $250/month for MVP workload

---

## 4. Reliability gate

- [ ] ALB health check hits `/health` → 200 OK
- [ ] ECS API service is `ACTIVE` with 1/1 running tasks
- [ ] ECS worker-parse service is `ACTIVE`
- [ ] ECS worker-investigate service is `ACTIVE`
- [ ] ECS Qdrant service is `ACTIVE`
- [ ] RDS instance status: `available`
- [ ] ElastiCache cluster status: `available`
- [ ] Celery worker heartbeat visible in logs: `worker-parse` and `worker-investigate`
- [ ] DLQ queues exist in Redis: `celery.parse.dlq`, `celery.investigate.dlq` (length = 0 initially)
- [ ] CloudWatch log groups receiving events (not empty after startup)
- [ ] SNS topic has a confirmed email subscription

### Run end-to-end smoke test:
```bash
# 1. Upload a known-good log file
FLIGHT=$(curl -s -X POST https://api.yourdomain.com/api/v1/flights/upload \
  -H "X-API-Key: $API_KEY" \
  -F "file=@logs/gps_crash_006.bin" | jq -r .flight_id)
echo "Flight ID: $FLIGHT"

# 2. Wait for parsing
sleep 60
STATUS=$(curl -s -H "X-API-Key: $API_KEY" https://api.yourdomain.com/api/v1/flights/$FLIGHT | jq -r .status)
echo "Parse status: $STATUS"  # Expected: ready

# 3. Start investigation
INV=$(curl -s -X POST https://api.yourdomain.com/api/v1/investigations \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"flight_id\": \"$FLIGHT\"}" | jq -r .investigation_id)
echo "Investigation ID: $INV"

# 4. Wait for investigation to complete (10-20 min)
# Monitor: curl -H "X-API-Key: $API_KEY" https://api.yourdomain.com/api/v1/investigations/$INV | jq .status
```

---

## 5. Inference provider gate

- [ ] Anthropic API key valid: test with `curl https://api.anthropic.com/v1/messages -H "x-api-key: $KEY" ...`
- [ ] OpenAI API key valid: test with `curl https://api.openai.com/v1/models -H "Authorization: Bearer $KEY"`
- [ ] Investigation worker logs show `inference_client_ready` with `providers=['anthropic','ollama','openai']`
- [ ] Investigation worker logs show `agent_routing_table` with correct providers
- [ ] No `routing_config_warning` entries in logs
- [ ] `InferenceMode.API` confirmed in health check: `GET /api/v1/health | jq .inference_mode`

---

## 6. Rollback plan

**Defined before every deploy. Never deploy without this.**

| Scenario | Rollback Action | Time to recover |
|---|---|---|
| Bad ECS deploy (task crash loop) | `aws ecs update-service --task-definition <prev_revision>` | 2-3 min |
| Bad DB migration | Restore from automated snapshot (7-day retention) | 15-30 min |
| Bad LLM API key | `aws secretsmanager put-secret-value ...` + force ECS redeploy | 3-5 min |
| Frontend broken | `aws s3 sync <prev_build_dir>/ s3://$BUCKET/` + CloudFront invalidation | 2-5 min |
| Complete infrastructure failure | `terraform apply` from last known-good state | 20-30 min |
| Runaway LLM spend | Revoke API key in Anthropic/OpenAI portal; set `rate_limit_investigations_per_day=0` in env | Immediate |

---

## 7. Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Engineering lead | | | |
| Security reviewer | | | |
| Product owner | | | |

**Result:** ☐ GO  ☐ NO-GO  ☐ CONDITIONAL GO (with written mitigations below)

**Notes / mitigations:**
```
(Document any WAIVEDs and their rationale here)
```
