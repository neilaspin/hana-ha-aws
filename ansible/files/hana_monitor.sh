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
