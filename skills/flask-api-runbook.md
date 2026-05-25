# Flask API — SRE Investigation Runbook

This runbook tells the AWS DevOps Agent how to investigate incidents on the
`ec2-flask-api` service.

---

## Application overview

| Property | Value |
|---|---|
| Service name | `ec2-flask-api` |
| Runtime | Python 3 / Flask, served by gunicorn (2 workers) |
| Host | EC2 t3.micro, Amazon Linux 2023 |
| Port | 5000 |
| Process PID file | `/var/run/flask-app.pid` |
| App log | `/var/log/flask-app.log` |
| App directory | `/opt/flask-app` |

### Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Health check — always returns 200 when app is running |
| `/items` | GET | Returns in-memory item list |
| `/items` | POST | Appends a new item |
| `/chaos` | GET | Returns 500 + burns CPU when `CHAOS_MODE=1` env var is set |

---

## Normal baselines

| Signal | Healthy range |
|---|---|
| EC2 CPUUtilization | < 20% |
| FlaskApp/ErrorCount | 0 per minute |
| StatusCheckFailed | 0 |
| `/health` HTTP response | 200 in < 100 ms |

---

## Step 1 — Confirm the alarm signal

Check the two primary CloudWatch metrics for the last 30 minutes:

1. `AWS/EC2 CPUUtilization` — Namespace `AWS/EC2`, dimension `InstanceId`
2. `FlaskApp/ErrorCount` — Namespace `FlaskApp`, no dimensions

Look for:
- A sudden spike starting at a specific timestamp (correlates with a deployment or config change)
- Whether both metrics spiked together (chaos mode) or only CPU (resource issue)
- Whether errors have since recovered (transient) or are still elevated (ongoing)

---

## Step 2 — Correlate with recent deployments

Query GitHub for any push or merge to the `main` branch in the **30 minutes before the alarm fired**.

- Repository: the repo linked to this Agent Space
- Look at commit messages and changed files for clues:
  - Changes to `app.py` → possible bad code deployment
  - Changes to EC2 user data / environment variables → possible CHAOS_MODE leak
  - No recent push → rule out deployment; focus on infrastructure

---

## Step 3 — Check the EC2 instance environment

SSH to the EC2 instance and run:

```bash
# Is CHAOS_MODE set?
printenv CHAOS_MODE

# Is gunicorn running?
cat /var/run/flask-app.pid && ps aux | grep gunicorn

# Recent app errors
tail -100 /var/log/flask-app.log

# Current CPU load
top -bn1 | head -5
```

---

## Common root causes and remediations

### Root cause 1 — CHAOS_MODE=1 is set on the EC2 instance

**Symptoms:** Both CPU and ErrorCount spike simultaneously; no recent bad deployment.

**Remediation:**
```bash
# Unset the env var for the running gunicorn process
sudo systemctl stop flask-app 2>/dev/null || sudo kill $(cat /var/run/flask-app.pid)
sudo -u ec2-user CHAOS_MODE=0 gunicorn --workers 2 --bind 0.0.0.0:5000 \
  --daemon --pid /var/run/flask-app.pid --log-file /var/log/flask-app.log \
  --chdir /opt/flask-app app:app
```

Verify recovery: `curl http://<instance-ip>:5000/chaos` should return 200 with `{"chaos":"inactive"}`.

---

### Root cause 2 — Bad code deployment via CodeDeploy

**Symptoms:** Error spike starts immediately after a GitHub push; app log shows tracebacks.

**Remediation:**
1. Open the AWS CodeDeploy console → Deployments
2. Identify the failed or most recent deployment ID
3. Click **Stop and roll back deployment** — CodeDeploy will re-deploy the last successful revision
4. Alternatively, revert the commit on GitHub and trigger a new pipeline run

---

### Root cause 3 — EC2 instance under-sized or instance health issue

**Symptoms:** CPU sustained above 70% with no error count spike; StatusCheckFailed alarm also fires.

**Remediation:**
1. Stop the instance
2. Change instance type to t3.small or t3.medium via **Actions → Instance Settings → Change instance type**
3. Start the instance; CodeDeploy agent will restart automatically
4. Confirm CPU returns to baseline within 5 minutes

---

## Escalation

If none of the above remediations resolve the incident within 15 minutes:
- Page the on-call engineer via the Slack `#incidents` channel
- Attach: alarm name, timestamp of first spike, GitHub commits from the window, and gunicorn log tail
