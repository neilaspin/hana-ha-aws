import boto3
import json
import os
import stat
import tempfile
import time
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS service clients — initialised once at cold start, reused across invocations
r53      = boto3.client('route53')
ec2      = boto3.client('ec2')
secrets  = boto3.client('secretsmanager')

# Required env vars — Lambda will error on cold start if any are missing
PRIMARY_INSTANCE_ID   = os.environ['PRIMARY_INSTANCE_ID']
SECONDARY_INSTANCE_ID = os.environ['SECONDARY_INSTANCE_ID']
HOSTED_ZONE_ID        = os.environ['HOSTED_ZONE_ID']
HANA_SID              = os.environ.get('HANA_SID', 'HDB')
HANA_INSTANCE         = os.environ.get('HANA_INSTANCE', '00')
HANA_HOSTNAME         = os.environ['HANA_HOSTNAME']       # e.g. hana-primary.hana.internal
SSH_KEY_SECRET_ARN    = os.environ['SSH_KEY_SECRET_ARN']  # ARN of the PEM key in Secrets Manager
SSH_USER              = os.environ.get('SSH_USER', 'ec2-user')


def get_ssh_key_file():
    # Fetch the PEM key from Secrets Manager and write it to a temp file.
    # Lambda has no persistent filesystem so we use /tmp via NamedTemporaryFile.
    # chmod 400 is required — SSH rejects keys with loose permissions.
    secret = secrets.get_secret_value(SecretId=SSH_KEY_SECRET_ARN)
    key_material = secret.get('SecretString') or secret['SecretBinary'].decode()
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
    f.write(key_material)
    f.close()
    os.chmod(f.name, stat.S_IRUSR)
    return f.name


def get_public_ip(instance_id):
    # Used to SSH into instances — public IP changes on stop/start so we always look it up live
    r = ec2.describe_instances(InstanceIds=[instance_id])
    return r['Reservations'][0]['Instances'][0]['PublicIpAddress']


def get_private_ip(instance_id):
    # Used for Route 53 records and HSR registration — private IPs are fixed
    r = ec2.describe_instances(InstanceIds=[instance_id])
    return r['Reservations'][0]['Instances'][0]['PrivateIpAddress']


def ssh_run(host, key_file, command, timeout=300):
    # paramiko imported here rather than at module level — keeps cold start lean
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
    # UPSERT updates the record if it exists, creates it if not.
    # TTL 30s means clients reconnect to the new primary within 30 seconds.
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
    # Returns the private IP currently registered as primary in Route 53.
    # This is how we determine role — whichever node's IP is in DNS is the current primary.
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
    # The failing node determines which node we target for takeover
    target_id = SECONDARY_INSTANCE_ID if failing_instance_id == PRIMARY_INSTANCE_ID \
                else PRIMARY_INSTANCE_ID
    logger.info(f"Initiating HSR takeover: {failing_instance_id} down, target={target_id}")
    key_file  = get_ssh_key_file()
    target_ip = get_public_ip(target_id)

    # sr_takeover promotes the secondary to primary without needing the old primary to respond
    rc, out, err = ssh_run(
        target_ip, key_file,
        f"sudo su - {HANA_SID.lower()}adm -c 'hdbnsutil -sr_takeover'",
    )
    logger.info(f"Takeover stdout: {out}")
    if err:
        logger.warning(f"Takeover stderr: {err}")
    if rc != 0:
        raise RuntimeError(f"sr_takeover failed (rc={rc}): {err}")

    # Update DNS to point to the new primary's private IP
    new_primary_ip = get_private_ip(target_id)
    update_route53(new_primary_ip)
    logger.info(f"Failover complete — new primary: {target_id} ({new_primary_ip})")

    # Attempt immediate re-registration of the failed node as new secondary.
    # If it fails (node not yet reachable), it will be retried when the instance
    # comes back up and fires a 'running' EventBridge event.
    try:
        handle_reregistration(failing_instance_id)
    except Exception as e:
        logger.warning(f"Re-registration of {failing_instance_id} failed: {e} — will retry on next boot")


def handle_secondary_restart(instance_id):
    # Called when the CloudWatch alarm fires for the secondary node.
    # The secondary crashing doesn't require a failover — just restart HANA.
    logger.info(f"Restarting HANA on secondary {instance_id}")
    key_file = get_ssh_key_file()
    ip = get_public_ip(instance_id)
    rc, out, err = ssh_run(ip, key_file,
        f"sudo su - {HANA_SID.lower()}adm -c 'HDB start'", timeout=600)
    if rc != 0:
        raise RuntimeError(f"HDB start failed on secondary (rc={rc}): {err}")
    logger.info(f"HANA restarted on secondary {instance_id}")


def handle_reregistration(returning_id):
    # Re-registers a returning node as HSR secondary and starts HANA replication.
    # Called either immediately after failover, or when EventBridge fires a 'running' event.
    logger.info(f"Re-registering {returning_id} as HSR secondary")

    current_primary_id = SECONDARY_INSTANCE_ID if returning_id == PRIMARY_INSTANCE_ID \
                         else PRIMARY_INSTANCE_ID
    site_name          = 'SITE1' if returning_id == PRIMARY_INSTANCE_ID else 'SITE2'
    primary_private_ip = get_private_ip(current_primary_id)
    # HANA HSR requires the primary hostname in AWS EC2 ip-x-x-x-x format
    primary_hostname   = "ip-" + primary_private_ip.replace('.', '-')

    key_file     = get_ssh_key_file()
    returning_ip = None

    # Wait up to 3 minutes for SSH to become available on the returning node
    for attempt in range(12):
        try:
            returning_ip = get_public_ip(returning_id)
            rc, _, _ = ssh_run(returning_ip, key_file, "echo ready", timeout=15)
            if rc == 0:
                break
        except Exception as e:
            logger.info(f"SSH not ready on {returning_id} (attempt {attempt+1}/12): {e}")
            time.sleep(15)

    # Stop HANA if it's still running — sr_register requires HANA to be down
    ssh_run(returning_ip, key_file,
        f"sudo su - {HANA_SID.lower()}adm -c 'HDB stop' 2>&1 || true")

    # Poll until hdbdaemon is gone — sr_register fails if any HANA process lingers
    for _ in range(40):
        rc, out, _ = ssh_run(returning_ip, key_file,
            f"sudo su - {HANA_SID.lower()}adm -c 'HDB info' 2>&1 || true", timeout=30)
        if 'hdbdaemon' not in out:
            break
        time.sleep(15)
    else:
        raise RuntimeError("HANA did not stop within 10 minutes")

    # Register this node as secondary, pointing at the current primary
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

    # Start HANA — it will come up as secondary and begin log replay from the primary
    ssh_run(returning_ip, key_file,
        f"sudo su - {HANA_SID.lower()}adm -c 'HDB start'",
        timeout=600)

    logger.info(f"Re-registration complete for {returning_id}")


def handler(event, context):
    logger.info(f"Event: {json.dumps(event)}")

    # --- Path 1: EventBridge EC2 state-change notification ---
    # Fires within seconds of an instance stopping, terminating, or coming back up
    if event.get('source') == 'aws.ec2':
        detail      = event.get('detail', {})
        state       = detail.get('state')
        instance_id = detail.get('instance-id')

        if state in ('stopped', 'terminated', 'stopping') \
                and instance_id in (PRIMARY_INSTANCE_ID, SECONDARY_INSTANCE_ID):
            # Only failover if the stopping instance is the current primary.
            # If it's the secondary going down, no action needed.
            if get_r53_ip() == get_private_ip(instance_id):
                handle_failover(instance_id)
            else:
                logger.info(f"{instance_id} stopped but is not current primary, no action")

        elif state == 'running' \
                and instance_id in (PRIMARY_INSTANCE_ID, SECONDARY_INSTANCE_ID):
            # Instance has come back up — re-register as secondary if it's not current primary
            if get_r53_ip() != get_private_ip(instance_id):
                handle_reregistration(instance_id)
            else:
                logger.info(f"{instance_id} is already current primary, no action")
        return

    # --- Path 2: CloudWatch alarm via SNS ---
    # Fires when HANARunning metric drops to 0 for 3 consecutive 10-second periods (~30s detection).
    # Catches HDB process crashes where the OS and EC2 instance remain up.
    if 'Records' in event:
        for record in event['Records']:
            if record.get('EventSource') == 'aws:sns':
                message     = json.loads(record['Sns']['Message'])
                if message.get('NewStateValue') == 'ALARM':
                    dims        = message.get('Trigger', {}).get('Dimensions', [])
                    instance_id = next(
                        (d['value'] for d in dims if d['name'] == 'InstanceId'), None)
                    if instance_id and get_r53_ip() == get_private_ip(instance_id):
                        # HANA down on the primary — trigger full failover
                        handle_failover(instance_id)
                    elif instance_id:
                        # HANA down on the secondary — just restart it
                        logger.info(f"HANA alarm for {instance_id} — secondary, restarting HANA")
                        handle_secondary_restart(instance_id)
                    else:
                        logger.warning("HANA alarm with no InstanceId dimension")
        return

    logger.warning("Unrecognized event format")
