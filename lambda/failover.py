import boto3
import json
import os
import stat
import tempfile
import time
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

r53 = boto3.client('route53')
ec2 = boto3.client('ec2')
secrets = boto3.client('secretsmanager')

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
    target_id = SECONDARY_INSTANCE_ID if failing_instance_id == PRIMARY_INSTANCE_ID else PRIMARY_INSTANCE_ID
    logger.info(f"Initiating HSR takeover: {failing_instance_id} down, target={target_id}")
    key_file = get_ssh_key_file()
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

    current_primary_id = SECONDARY_INSTANCE_ID if returning_id == PRIMARY_INSTANCE_ID else PRIMARY_INSTANCE_ID
    site_name          = 'SITE1' if returning_id == PRIMARY_INSTANCE_ID else 'SITE2'
    primary_private_ip = get_private_ip(current_primary_id)
    primary_hostname   = "ip-" + primary_private_ip.replace('.', '-')

    key_file = get_ssh_key_file()
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
        detail = event.get('detail', {})
        state = detail.get('state')
        instance_id = detail.get('instance-id')

        if state in ('stopped', 'terminated', 'stopping') and instance_id in (PRIMARY_INSTANCE_ID, SECONDARY_INSTANCE_ID):
            if get_r53_ip() == get_private_ip(instance_id):
                handle_failover(instance_id)
            else:
                logger.info(f"{instance_id} stopped but is not current primary, no action")
        elif state == 'running' and instance_id in (PRIMARY_INSTANCE_ID, SECONDARY_INSTANCE_ID):
            if get_r53_ip() != get_private_ip(instance_id):
                handle_reregistration(instance_id)
            else:
                logger.info(f"{instance_id} is already current primary, no action")
        return

    if 'Records' in event:
        for record in event['Records']:
            if record.get('EventSource') == 'aws:sns':
                message = json.loads(record['Sns']['Message'])
                if message.get('NewStateValue') == 'ALARM':
                    dims = message.get('Trigger', {}).get('Dimensions', [])
                    instance_id = next((d['value'] for d in dims if d['name'] == 'InstanceId'), None)
                    if instance_id and get_r53_ip() == get_private_ip(instance_id):
                        handle_failover(instance_id)
                    elif instance_id:
                        logger.info(f"HANA alarm for {instance_id} — secondary, restarting HANA")
                        handle_secondary_restart(instance_id)
                    else:
                        logger.warning("HANA alarm with no InstanceId dimension")
        return

    logger.warning("Unrecognized event format")
