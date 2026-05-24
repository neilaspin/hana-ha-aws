#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/terraform"

PRIMARY_PUBLIC=$(terraform output -raw primary_public_ip 2>&1)
SECONDARY_PUBLIC=$(terraform output -raw secondary_public_ip 2>&1)
PRIMARY_PRIVATE=$(terraform output -raw primary_private_ip 2>&1)

if [[ ! "$PRIMARY_PUBLIC" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ || ! "$SECONDARY_PUBLIC" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "ERROR: No valid IPs from Terraform — run terraform apply first"
  exit 1
fi

PRIMARY_HOSTNAME="ip-$(echo "$PRIMARY_PRIVATE" | tr '.' '-')"

cat > "$SCRIPT_DIR/ansible/inventory.ini" <<EOF
[primary]
hana-primary ansible_host=${PRIMARY_PUBLIC} ansible_user=ec2-user ansible_ssh_private_key_file=~/.ssh/HANA_DEP.pem

[secondary]
hana-secondary ansible_host=${SECONDARY_PUBLIC} ansible_user=ec2-user ansible_ssh_private_key_file=~/.ssh/HANA_DEP.pem

[hana:children]
primary
secondary

[hana:vars]
ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=30 -o ServerAliveCountMax=20'
primary_private_hostname=${PRIMARY_HOSTNAME}
EOF

echo "inventory.ini updated — primary: ${PRIMARY_PUBLIC} (${PRIMARY_HOSTNAME}), secondary: ${SECONDARY_PUBLIC}"
