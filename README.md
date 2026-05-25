SAP HANA High Availability (...Well, my version) on AWS Using EventBridge and Lambda

Deployment Steps
1. Deploy infrastructure

cd terraform
terraform apply

Note: only run this once. After initial deployment, never run `terraform apply` again — use AWS CLI for any updates.

2. Generate inventory

./generate_inventory.sh

3. Install HANA on both nodes

cd ansible
ansible-playbook -i inventory.ini hana-install.yml

4. Configure HSR

ansible-playbook -i inventory.ini hsr-setup.yml

5. Build and deploy the Lambda package

cd ..
./build.sh
cd lambda/package && zip -r ../lambda_failover.zip .
aws lambda update-function-code --function-name hana-failover --zip-file fileb://../lambda_failover.zip --region us-east-2

6. Deploy the HANA process monitor to both nodes

cd ansible
ansible-playbook -i inventory.ini hana-monitor.yml

7. Create the CloudWatch alarms

for INSTANCE in <primary-id> <secondary-id>; do
  aws cloudwatch put-metric-alarm \
    --alarm-name "hana-process-failed-${INSTANCE}" \
    --namespace HANA/Health \
    --metric-name HANARunning \
    --dimensions Name=InstanceId,Value="${INSTANCE}" \
    --period 10 --evaluation-periods 3 \
    --statistic Maximum --threshold 1 \
    --comparison-operator LessThanThreshold \
    --treat-missing-data ignore \
    --alarm-actions arn:aws:sns:us-east-2:<account-id>:hana-failover \
    --region us-east-2
done
