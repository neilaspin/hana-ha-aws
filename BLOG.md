# SAP HANA High Availability on AWS — Without Pacemaker

## Overview

I don't like Pacemaker clusters.

They work until they don't and then you usually lose everything. Putting Pacemaker into a cloud-native environment has always felt like riding a horse on a motorway. STONITH configuration, split-brain handling, cluster resource agents, corosync tuning, and documentation that assumes you already know what you're doing. The learning curve is steep, the failure modes are entertaining, and none of it feels like it belongs on a cloud-based solution.

So I built something different. No cluster software on the instances at all. The HA logic lives entirely in AWS-native services — EventBridge, Lambda, CloudWatch, and Route 53 — and delivers the same outcome: automated failover, HSR takeover, and automatic re-registration of the failed node as the new secondary when it recovers.

The solution handles two failure scenarios:
- **OS/instance failure** — detected in seconds via Amazon EventBridge EC2 state-change notifications
- **HANA process crash** — detected within 30 seconds via a custom CloudWatch metric pushed by a lightweight monitor running on each node

Both trigger the same automated response: HSR takeover on the surviving node, Route 53 DNS update, re-registration of the failed node as secondary when it comes back.

Full repo: https://github.com/neilaspin/hana-ha-aws

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                        AWS VPC                           │
│                                                          │
│  ┌──────────────┐    HSR (syncmem)    ┌──────────────┐  │
│  │  HANA SITE1  │◄───────────────────►│  HANA SITE2  │  │
│  │  (primary)   │                     │  (secondary) │  │
│  └──────────────┘                     └──────────────┘  │
│         │                                    │           │
│         └──────────────────────────────────┬─┘           │
│                    CloudWatch              │             │
│                  Custom Metrics            │             │
└────────────────────────────────────────────┼─────────────┘
                                             │
                    ┌────────────────────────┼──────────────┐
                    │                        │              │
              EventBridge              CloudWatch        Route 53
           (EC2 state change)      (HANA process alarm)  (private zone)
                    │                        │              │
                    └────────────┬───────────┘              │
                                 │                          │
                          Lambda Function                   │
                        (hana-failover)                     │
                                 │                          │
                                 └──────────────────────────┘
                                    updates DNS on failover
```

**Components:**

| Component | Role |
|-----------|------|
| EC2 (r5.xlarge, SLES for SAP) | HANA nodes — one per AZ |
| HANA System Replication (HSR) | syncmem / logreplay mode |
| Amazon EventBridge | Detects EC2 instance state changes in seconds |
| Amazon CloudWatch | Custom metric from HANA process monitor (10s interval) |
| AWS Lambda (Python 3.12) | Orchestrates takeover and re-registration via SSH |
| Route 53 private hosted zone | Stable DNS endpoint for HANA clients |
| AWS Secrets Manager | Stores SSH private key for Lambda |

---

## How It Works

### Failure Detection

Two complementary detection paths run in parallel:

**Path 1 — EC2 instance failure (seconds)**

Amazon EventBridge fires the moment an EC2 instance transitions to `stopping`, `stopped`, or `terminated`. This covers OS panics, instance stop/terminate, and hardware failure that takes down the hypervisor. There is no polling — the event is push-based and arrives at the Lambda within one to two seconds of the state change.

**Path 2 — HANA process crash (≤30 seconds)**

A lightweight shell script runs as a systemd service on each node. Every 10 seconds it checks whether `hdbnameserver` is running and pushes a high-resolution custom metric (`HANA/Health :: HANARunning`) to CloudWatch with a value of 1 (healthy) or 0 (down). A CloudWatch alarm evaluates three consecutive 10-second periods — if it sees three zeros, it fires via SNS to the Lambda.

This path catches the scenario that EC2 monitoring cannot: `HDB kill-9` on a running instance. The OS stays up, the EC2 status check stays green, but HANA is dead.

The screenshot below shows the moment this alarm fires. The left alarm (`hana-process-failed-i-0ebf62666d...`) has turned red — HANA is down on SITE1. The right alarm (SITE2) remains green, confirming the secondary is healthy and ready for takeover.

![CloudWatch alarm firing — HANA process failure detected on SITE1](./screenshots/cloudwatch-alarm-firing.png)

```bash
# /usr/local/bin/hana_monitor.sh — runs as systemd service
#!/bin/bash
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region)

while true; do
    if pgrep -f hdbnameserver > /dev/null 2>&1; then
        VALUE=1
    else
        VALUE=0
    fi

    aws cloudwatch put-metric-data \
      --namespace HANA/Health \
      --metric-name HANARunning \
      --dimensions InstanceId="$INSTANCE_ID" \
      --value "$VALUE" \
      --storage-resolution 1 \
      --region "$REGION"

    sleep 10
done
```

```ini
# /etc/systemd/system/hana-monitor.service
[Unit]
Description=HANA Process Monitor
After=network.target

[Service]
ExecStart=/usr/local/bin/hana_monitor.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Failover

When the Lambda receives either trigger, it:

1. Checks Route 53 to confirm the failing instance is the current primary (not the secondary)
2. SSHes to the surviving node using the key from Secrets Manager
3. Runs `hdbnsutil -sr_takeover`
4. Updates the Route 53 A record to point to the new primary's private IP
5. Immediately attempts re-registration of the failed node as the new secondary

The Route 53 TTL is set to 30 seconds. HANA clients using the DNS name reconnect to the new primary as soon as their connection drops and DNS resolves.

### Re-registration

After the takeover, the Lambda SSHes into the former primary and:

1. Stops HANA (or confirms it is already stopped)
2. Waits for all HANA processes to exit cleanly
3. Runs `hdbnsutil -sr_register` pointing at the new primary
4. Starts HANA — it comes up as secondary and begins log replay

If re-registration fails (e.g., the instance is not yet reachable), the Lambda logs a warning and the process completes on the next EC2 `running` state-change event when the instance recovers.

### Role Awareness

The Lambda does not hardcode which node is primary. Instead it queries Route 53 on every invocation. Whichever node's private IP matches the current Route 53 record is the primary — that is the node whose failure triggers a takeover. This means the solution works correctly across multiple failovers in either direction without any reconfiguration.

---

## The Lambda Function

```python
import boto3
import json
import os
import stat
import tempfile
import time
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

r53      = boto3.client('route53')
ec2      = boto3.client('ec2')
secrets  = boto3.client('secretsmanager')

PRIMARY_INSTANCE_ID   = os.environ['PRIMARY_INSTANCE_ID']
SECONDARY_INSTANCE_ID = os.environ['SECONDARY_INSTANCE_ID']
HOSTED_ZONE_ID        = os.environ['HOSTED_ZONE_ID']
HANA_SID              = os.environ.get('HANA_SID', 'HDB')
HANA_INSTANCE         = os.environ.get('HANA_INSTANCE', '00')
HANA_HOSTNAME         = os.environ['HANA_HOSTNAME']
SSH_KEY_SECRET_ARN    = os.environ['SSH_KEY_SECRET_ARN']
SSH_USER              = os.environ.get('SSH_USER', 'ec2-user')


def get_ssh_key_file():
    secret = secrets.get_secret_value(SecretId=SSH_KEY_SECRET_ARN)
    key_material = secret.get('SecretString') or secret['SecretBinary'].decode()
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
    f.write(key_material)
    f.close()
    os.chmod(f.name, stat.S_IRUSR)
    return f.name


def get_public_ip(instance_id):
    r = ec2.describe_instances(InstanceIds=[instance_id])
    return r['Reservations'][0]['Instances'][0]['PublicIpAddress']


def get_private_ip(instance_id):
    r = ec2.describe_instances(InstanceIds=[instance_id])
    return r['Reservations'][0]['Instances'][0]['PrivateIpAddress']


def ssh_run(host, key_file, command, timeout=300):
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=SSH_USER, key_filename=key_file, timeout=30)
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    rc  = stdout.channel.recv_exit_status()
    out = stdout.read().decode()
    err = stderr.read().decode()
    client.close()
    return rc, out, err


def update_route53(new_ip):
    r53.change_resource_record_sets(
        HostedZoneId=HOSTED_ZONE_ID,
        ChangeBatch={
            'Changes': [{
                'Action': 'UPSERT',
                'ResourceRecordSet': {
                    'Name': HANA_HOSTNAME,
                    'Type': 'A',
                    'TTL': 30,
                    'ResourceRecords': [{'Value': new_ip}],
                }
            }]
        }
    )
    logger.info(f"Route 53 updated: {HANA_HOSTNAME} -> {new_ip}")


def get_r53_ip():
    response = r53.list_resource_record_sets(
        HostedZoneId=HOSTED_ZONE_ID,
        StartRecordName=HANA_HOSTNAME,
        StartRecordType='A',
        MaxItems='1',
    )
    for rrset in response['ResourceRecordSets']:
        if rrset['Name'].rstrip('.') == HANA_HOSTNAME.rstrip('.'):
            return rrset['ResourceRecords'][0]['Value']
    return None


def handle_failover(failing_instance_id):
    target_id = SECONDARY_INSTANCE_ID if failing_instance_id == PRIMARY_INSTANCE_ID \
                else PRIMARY_INSTANCE_ID
    logger.info(f"Initiating HSR takeover: {failing_instance_id} down, target={target_id}")
    key_file  = get_ssh_key_file()
    target_ip = get_public_ip(target_id)

    rc, out, err = ssh_run(
        target_ip, key_file,
        f"sudo su - {HANA_SID.lower()}adm -c 'hdbnsutil -sr_takeover'",
    )
    logger.info(f"Takeover stdout: {out}")
    if err:
        logger.warning(f"Takeover stderr: {err}")
    if rc != 0:
        raise RuntimeError(f"sr_takeover failed (rc={rc}): {err}")

    new_primary_ip = get_private_ip(target_id)
    update_route53(new_primary_ip)
    logger.info(f"Failover complete — new primary: {target_id} ({new_primary_ip})")

    try:
        handle_reregistration(failing_instance_id)
    except Exception as e:
        logger.warning(f"Re-registration of {failing_instance_id} failed: {e} — will retry on next boot")


def handle_secondary_restart(instance_id):
    logger.info(f"Restarting HANA on secondary {instance_id}")
    key_file = get_ssh_key_file()
    ip = get_public_ip(instance_id)
    rc, out, err = ssh_run(ip, key_file,
        f"sudo su - {HANA_SID.lower()}adm -c 'HDB start'", timeout=600)
    if rc != 0:
        raise RuntimeError(f"HDB start failed on secondary (rc={rc}): {err}")
    logger.info(f"HANA restarted on secondary {instance_id}")


def handle_reregistration(returning_id):
    logger.info(f"Re-registering {returning_id} as HSR secondary")

    current_primary_id = SECONDARY_INSTANCE_ID if returning_id == PRIMARY_INSTANCE_ID \
                         else PRIMARY_INSTANCE_ID
    site_name          = 'SITE1' if returning_id == PRIMARY_INSTANCE_ID else 'SITE2'
    primary_private_ip = get_private_ip(current_primary_id)
    primary_hostname   = "ip-" + primary_private_ip.replace('.', '-')

    key_file     = get_ssh_key_file()
    returning_ip = None

    for attempt in range(12):
        try:
            returning_ip = get_public_ip(returning_id)
            rc, _, _ = ssh_run(returning_ip, key_file, "echo ready", timeout=15)
            if rc == 0:
                break
        except Exception as e:
            logger.info(f"SSH not ready on {returning_id} (attempt {attempt+1}/12): {e}")
            time.sleep(15)

    ssh_run(returning_ip, key_file,
        f"sudo su - {HANA_SID.lower()}adm -c 'HDB stop' 2>&1 || true")

    for _ in range(40):
        rc, out, _ = ssh_run(returning_ip, key_file,
            f"sudo su - {HANA_SID.lower()}adm -c 'HDB info' 2>&1 || true", timeout=30)
        if 'hdbdaemon' not in out:
            break
        time.sleep(15)
    else:
        raise RuntimeError("HANA did not stop within 10 minutes")

    rc, out, err = ssh_run(
        returning_ip, key_file,
        f'sudo su - {HANA_SID.lower()}adm -c "'
        f'hdbnsutil -sr_register'
        f' --name={site_name}'
        f' --remoteHost={primary_hostname}'
        f' --remoteInstance={HANA_INSTANCE}'
        f' --replicationMode=syncmem'
        f' --operationMode=logreplay"',
    )
    if rc != 0:
        raise RuntimeError(f"sr_register failed (rc={rc}): {err}")

    ssh_run(returning_ip, key_file,
        f"sudo su - {HANA_SID.lower()}adm -c 'HDB start'",
        timeout=600)

    logger.info(f"Re-registration complete for {returning_id}")


def handler(event, context):
    logger.info(f"Event: {json.dumps(event)}")

    if event.get('source') == 'aws.ec2':
        detail      = event.get('detail', {})
        state       = detail.get('state')
        instance_id = detail.get('instance-id')

        if state in ('stopped', 'terminated', 'stopping') \
                and instance_id in (PRIMARY_INSTANCE_ID, SECONDARY_INSTANCE_ID):
            if get_r53_ip() == get_private_ip(instance_id):
                handle_failover(instance_id)
            else:
                logger.info(f"{instance_id} stopped but is not current primary, no action")

        elif state == 'running' \
                and instance_id in (PRIMARY_INSTANCE_ID, SECONDARY_INSTANCE_ID):
            if get_r53_ip() != get_private_ip(instance_id):
                handle_reregistration(instance_id)
            else:
                logger.info(f"{instance_id} is already current primary, no action")
        return

    if 'Records' in event:
        for record in event['Records']:
            if record.get('EventSource') == 'aws:sns':
                message     = json.loads(record['Sns']['Message'])
                if message.get('NewStateValue') == 'ALARM':
                    dims        = message.get('Trigger', {}).get('Dimensions', [])
                    instance_id = next(
                        (d['value'] for d in dims if d['name'] == 'InstanceId'), None)
                    if instance_id and get_r53_ip() == get_private_ip(instance_id):
                        handle_failover(instance_id)
                    elif instance_id:
                        logger.info(f"HANA alarm for {instance_id} — secondary, restarting HANA")
                        handle_secondary_restart(instance_id)
                    else:
                        logger.warning("HANA alarm with no InstanceId dimension")
        return

    logger.warning("Unrecognized event format")
```

**Building the Lambda package**

Paramiko (the SSH library) must be compiled for Linux x86_64, not the Mac or Windows platform where you build. Use pip's `--platform` flag to pull the correct binary:

```bash
#!/bin/bash
set -e
PACKAGE_DIR="./lambda/package"
rm -rf "$PACKAGE_DIR" && mkdir -p "$PACKAGE_DIR"
cp lambda/failover.py "$PACKAGE_DIR/"
pip install paramiko \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --only-binary=:all: \
  --target "$PACKAGE_DIR/"
cd "$PACKAGE_DIR" && zip -r ../lambda_failover.zip .
```

---

## Infrastructure (Terraform)

Key infrastructure excerpts — full Terraform is available in the companion repository.

**EventBridge — watches both nodes for instance state changes:**

```hcl
resource "aws_cloudwatch_event_rule" "primary_down" {
  name = "hana-primary-down"
  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["EC2 Instance State-change Notification"]
    detail = {
      state       = ["stopped", "terminated", "stopping"]
      instance-id = [aws_instance.hana_primary.id, aws_instance.hana_secondary.id]
    }
  })
}

resource "aws_cloudwatch_event_rule" "instance_running" {
  name = "hana-instance-running"
  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["EC2 Instance State-change Notification"]
    detail = {
      state       = ["running"]
      instance-id = [aws_instance.hana_primary.id, aws_instance.hana_secondary.id]
    }
  })
}
```

**Route 53 private hosted zone:**

```hcl
resource "aws_route53_zone" "hana" {
  name = var.private_zone_name   # e.g. hana.internal
  vpc {
    vpc_id = aws_vpc.hana.id
  }
}

resource "aws_route53_record" "primary" {
  zone_id = aws_route53_zone.hana.zone_id
  name    = "hana-primary.${var.private_zone_name}"
  type    = "A"
  ttl     = 30
  records = [aws_instance.hana_primary.private_ip]
}
```

**CloudWatch alarms — one per node, 10-second high-resolution periods:**

```bash
for INSTANCE in i-0xxxxxxxxxxxx i-0yyyyyyyyyyyy; do
  aws cloudwatch put-metric-alarm \
    --alarm-name "hana-process-failed-${INSTANCE}" \
    --namespace HANA/Health \
    --metric-name HANARunning \
    --dimensions Name=InstanceId,Value="${INSTANCE}" \
    --period 10 \
    --evaluation-periods 3 \
    --statistic Maximum \
    --threshold 1 \
    --comparison-operator LessThanThreshold \
    --treat-missing-data ignore \
    --alarm-actions arn:aws:sns:eu-west-1:123456789012:hana-failover \
    --region eu-west-1
done
```

> **Note:** `--treat-missing-data ignore` is important. Using `breaching` causes false alarms during the window between alarm creation and the first metric arriving.

---

## Tested Failure Scenarios

All scenarios below were tested live, with both nodes taking turns as primary to confirm fully bidirectional operation.

---

### Scenario 1: Killing the OS (EC2 instance stop)

The first test is stopping the EC2 instance itself — equivalent to a hard power-off or hypervisor failure.

```bash
aws ec2 stop-instances --instance-ids i-0xxxxxxxxxxxx
```

Amazon EventBridge fires the moment the instance transitions to `stopping`. The Lambda receives the event within one to two seconds, checks Route 53 to confirm the stopping instance is the current primary, and immediately SSHes to the surviving secondary to run `hdbnsutil -sr_takeover`. Route 53 is then updated to point to the new primary's private IP.

When the stopped instance comes back up, EventBridge fires a `running` event. The Lambda detects that its private IP no longer matches the Route 53 record (it is no longer primary), and automatically re-registers it as the new HSR secondary — no manual intervention required.

This scenario was tested in both directions. After SITE1 was stopped and SITE2 took over as primary, SITE2 was then stopped to confirm SITE1 could take over in the reverse direction.

**Detection time: ~2 seconds**

---

### Scenario 2: Killing the HANA process (HDB kill-9)

The more insidious failure is the HANA process dying while the OS remains up. The EC2 instance status checks stay green, EventBridge sees nothing — this is entirely invisible to AWS infrastructure monitoring.

```bash
sudo -u hdbadm HDB kill-9
```

The HANA process monitor detects `hdbnameserver` is no longer running within the next 10-second polling cycle. It pushes `HANARunning = 0` to CloudWatch. After three consecutive zero readings (30 seconds total), the CloudWatch alarm fires and delivers the notification to the Lambda via SNS.

The screenshot below shows this in action. The left alarm (`hana-process-failed-i-0ebf62666d...`) has turned red — HANA is down on SITE1. The right alarm (SITE2) remains green, confirming the secondary is healthy and ready for takeover.

![CloudWatch alarm firing — HANA process failure detected on SITE1](./screenshots/cloudwatch-alarm-firing.png)

The Lambda receives the SNS notification, confirms the failing node is the current primary via Route 53, and runs the takeover on SITE2. Once the takeover and re-registration complete, both alarms return to OK.

![CloudWatch — both alarms OK after successful failover and re-registration](./screenshots/cloudwatch-alarms-recovered.png)

The metric graphs show the characteristic signature: `HANARunning` drops to 0 during the failure, then returns to 1 once HANA is restarted on the re-registered secondary.

**Detection time: ~30 seconds**

---

### Summary

| Scenario | Detection method | Detection time | Result |
|----------|-----------------|----------------|--------|
| `aws ec2 stop-instances` | EventBridge | ~2 seconds | Takeover + re-registration ✓ |
| Instance hardware failure | EventBridge | ~2 seconds | Takeover + re-registration ✓ |
| `HDB kill-9` (OS stays up) | CloudWatch alarm | ~30 seconds | Takeover + re-registration ✓ |
| Secondary HANA crash | CloudWatch alarm | ~30 seconds | Automatic HDB start ✓ |

---

## What This Replaces

| Pacemaker component | AWS equivalent |
|--------------------|----------------|
| Cluster daemon (corosync/pacemaker) | EventBridge + Lambda |
| STONITH / fencing | Not required — Lambda only acts on the surviving node |
| Resource agent (SAPHana) | Lambda `handle_failover` / `handle_reregistration` |
| Cluster VIP | Route 53 private zone (TTL 30s) |
| Cluster logs (`crm_mon`) | CloudWatch Logs (`/aws/lambda/hana-failover`) |

No cluster software is installed on the HANA nodes. The nodes have no knowledge of each other beyond HSR itself.

---

## Limitations and Considerations

- **Split-brain**: Because the Lambda only acts on the surviving node (never both simultaneously), split-brain is not possible in this design.
- **SSH dependency**: The Lambda reaches the surviving node over public IP via SSH. If the surviving node's SSH is unreachable, the takeover will not complete. A VPN or Direct Connect removes this dependency.
- **Lambda timeout**: Set to 600 seconds. Re-registration including HANA startup can take several minutes; this gives comfortable headroom.
- **Re-registration timing**: If HANA restart takes longer than the Lambda timeout, re-registration will be retried on the next EC2 `running` event.
- **No SAP support statement**: This solution is not covered by an SAP support statement for HA. Pacemaker with the SAPHana resource agent remains the SAP-supported configuration.

---

## Summary

This solution demonstrates that SAP HANA HA on AWS does not require Pacemaker. Using EventBridge for sub-second EC2 failure detection, a custom CloudWatch metric for HANA process monitoring, and a single Lambda function for orchestration, it is possible to build a fully automated two-node cluster that handles both OS-level and application-level failures in both directions — with all cluster logic centralised in one place and no cluster software on the nodes themselves.
