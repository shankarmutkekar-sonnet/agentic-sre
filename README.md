# Agentic SRE — End-to-End Incident Response on AWS

Automatically detects incidents on a Flask API running on EC2, investigates root causes
using AWS DevOps Agent, and posts findings to Slack — with no human intervention.

```
Flask API (EC2) → CloudWatch Alarms → EventBridge → Lambda (HMAC webhook)
                                                           ↓
GitHub (deployment history) ←→ AWS DevOps Agent Space → Slack
```

---

## Prerequisites

| Tool | Version |
|---|---|
| AWS CLI | v2, configured with an IAM user/role that has admin or scoped permissions |
| Node.js | 20.x (for Lambda packaging) |
| Python | 3.11+ |
| Git | any recent version |

---

## 1 — GitHub repository setup

1. Create a new GitHub repository (e.g. `agentic-sre`).
2. Push this project to it:
   ```bash
   git init
   git remote add origin https://github.com/<you>/agentic-sre.git
   git add .
   git commit -m "initial commit"
   git push -u origin main
   ```
3. Note the repository URL — you will paste it into the DevOps Agent Space later.

---

## 2 — Launch the EC2 instance

1. Open the EC2 console → **Launch instance**
2. Settings:
   - **AMI:** Amazon Linux 2023
   - **Instance type:** t3.micro
   - **Key pair:** create or select one
   - **Security group:** allow inbound TCP 5000 + SSH 22 (your IP or 0.0.0.0/0 for testing)
3. Under **Advanced details → IAM instance profile**, attach a role with:
   - `CloudWatchAgentServerPolicy` — allows `PutMetricData`
   - `AmazonEC2RoleforAWSCodeDeploy` — allows CodeDeploy to deploy revisions
4. Launch and note the **Instance ID** (format: `i-0123456789abcdef0`).

---

## 3 — Install the CodeDeploy agent on EC2

SSH into the instance, then run:

```bash
sudo yum install -y ruby wget
cd /tmp
wget https://aws-codedeploy-us-east-1.s3.amazonaws.com/latest/install
chmod +x install
sudo ./install auto
sudo systemctl enable codedeploy-agent
sudo systemctl start codedeploy-agent
sudo systemctl status codedeploy-agent   # confirm: active (running)
```

> Change the S3 bucket region prefix if you are not using `us-east-1`.

---

## 4 — Set up CodeDeploy

1. Open **CodeDeploy → Applications → Create application**
   - Name: `flask-api`
   - Platform: EC2/On-premises
2. Create a **Deployment group**
   - Name: `flask-api-prod`
   - Service role: a role with `AWSCodeDeployRole` policy
   - Environment: EC2 instances — tag `Name = flask-api` (add this tag to your instance)
   - Deployment config: `CodeDeployDefault.AllAtOnce`
   - Disable load balancer (single instance)

---

## 5 — Deploy the CloudFormation stacks

### 5a — CloudWatch alarms (deploy first)

```bash
aws cloudformation deploy \
  --template-file infra/cloudwatch.yaml \
  --stack-name flask-api-alarms \
  --parameter-overrides EC2InstanceId=<your-instance-id>
```

### 5b — Lambda + EventBridge webhook forwarder (deploy second)

You need two values from the DevOps Agent Space console (set up in step 6):
- Webhook URL
- Webhook HMAC secret

```bash
aws cloudformation deploy \
  --template-file infra/template.yaml \
  --stack-name devops-agent-webhook \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      DevOpsAgentWebhookUrl="https://your-webhook-url" \
      DevOpsAgentWebhookSecret="your-secret" \
      AlarmName="flask-api-cpu-high"
```

---

## 6 — Set up the AWS DevOps Agent Space

1. Open the [AWS DevOps Agent console](https://console.aws.amazon.com/devopsagent).
2. **Create an Agent Space** — give it a name (e.g. `flask-sre`).
3. **Add a Webhook** integration:
   - Copy the **Webhook URL** and **HMAC secret** → use in step 5b above.
4. **Add GitHub** integration:
   - Connect your GitHub account and select the `agentic-sre` repository.
5. **Add CloudWatch** integration:
   - Grant the Agent Space read access to your CloudWatch metrics and alarms.
6. **Add Slack** integration:
   - Connect your Slack workspace and select the channel for incident notifications (e.g. `#incidents`).
7. **Add a Skill**:
   - Upload `skills/flask-api-runbook.md` as a new Skill named `Flask API Runbook`.

---

## 7 — Deploy the Flask app via CodeDeploy

Package the app directory and push a deployment:

```bash
aws deploy push \
  --application-name flask-api \
  --s3-location s3://<your-bucket>/flask-api.zip \
  --source app/

aws deploy create-deployment \
  --application-name flask-api \
  --deployment-group-name flask-api-prod \
  --s3-location bucket=<your-bucket>,key=flask-api.zip,bundleType=zip
```

Verify the app is running:

```bash
curl http://<ec2-public-ip>:5000/health
# → {"status": "ok"}
```

---

## 8 — Trigger a test incident

SSH into the EC2 instance and enable chaos mode:

```bash
# Find the gunicorn PID and restart with CHAOS_MODE=1
sudo kill $(cat /var/run/flask-app.pid)
sudo CHAOS_MODE=1 gunicorn \
  --workers 2 --bind 0.0.0.0:5000 \
  --daemon --pid /var/run/flask-app.pid \
  --log-file /var/log/flask-app.log \
  --chdir /opt/flask-app app:app
```

Then hit the chaos endpoint repeatedly to generate errors:

```bash
for i in $(seq 1 20); do curl -s http://localhost:5000/chaos; done
```

This emits 20 × `FlaskApp/ErrorCount` data points, which breaches the `flask-api-errors` alarm
(threshold: 10 in 1 minute) and triggers the full pipeline.

---

## 9 — What to expect

| Component | What happens |
|---|---|
| CloudWatch | `flask-api-errors` alarm transitions to ALARM state |
| EventBridge | Rule matches the alarm state-change event |
| Lambda | `devops-agent-webhook-forwarder` fires; signs and POSTs the incident to the DevOps Agent webhook |
| DevOps Agent | Queries CloudWatch metrics + GitHub for recent deployments; runs the Flask API Runbook skill |
| Slack | Agent posts root cause analysis + recommended remediation steps to `#incidents` |

Expected Slack message includes:
- Alarm name and timestamp
- CloudWatch metric chart link
- GitHub commit(s) from the 30-minute window before the alarm
- Root cause conclusion (e.g. "CHAOS_MODE=1 detected")
- Remediation steps

---

## Force-triggering the Lambda without a real alarm

Useful for testing the webhook end-to-end before any real incident:

```bash
aws lambda invoke \
  --function-name devops-agent-webhook-forwarder \
  --payload '{"source":"aws.cloudwatch","detail-type":"CloudWatch Alarm State Change","account":"<account-id>","region":"us-east-1","time":"2026-05-25T10:00:00Z","detail":{"alarmName":"flask-api-errors","alarmArn":"arn:aws:cloudwatch:us-east-1:<account-id>:alarm:flask-api-errors","state":{"value":"ALARM","reason":"Threshold crossed: 15 errors > 10 threshold"},"previousState":{"value":"OK"}}}' \
  response.json && cat response.json
```

Replace `<account-id>` with your 12-digit AWS account ID.

---

## Project structure

```
agentic-sre/
├── app/
│   ├── app.py               # Flask application (4 endpoints)
│   ├── requirements.txt     # flask, boto3, gunicorn
│   ├── appspec.yml          # CodeDeploy deployment spec
│   └── scripts/
│       ├── before_install.sh    # pip install dependencies
│       ├── application_start.sh # start gunicorn daemon
│       └── application_stop.sh  # stop gunicorn daemon
├── infra/
│   ├── template.yaml        # CloudFormation: Lambda + EventBridge + IAM
│   └── cloudwatch.yaml      # CloudFormation: EC2 alarms
├── lambda/
│   └── index.mjs            # Webhook forwarder Lambda
├── skills/
│   └── flask-api-runbook.md # DevOps Agent investigation runbook
└── README.md
```
