locals {
  sles_sap_ami  = "ami-099afc29551c4b139"
  hana_hostname = "hana-primary.${var.private_zone_name}"
}

# --- VPC and Networking ---

resource "aws_vpc" "hana" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = { Name = "hana-vpc" }
}

resource "aws_subnet" "primary" {
  vpc_id            = aws_vpc.hana.id
  cidr_block        = "10.0.1.0/24"
  availability_zone = "${var.aws_region}a"
  tags = { Name = "hana-primary-subnet" }
}

resource "aws_subnet" "secondary" {
  vpc_id            = aws_vpc.hana.id
  cidr_block        = "10.0.2.0/24"
  availability_zone = "${var.aws_region}b"
  tags = { Name = "hana-secondary-subnet" }
}

resource "aws_internet_gateway" "hana" {
  vpc_id = aws_vpc.hana.id
  tags   = { Name = "hana-igw" }
}

resource "aws_route_table" "hana" {
  vpc_id = aws_vpc.hana.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.hana.id
  }
  tags = { Name = "hana-rt" }
}

resource "aws_route_table_association" "primary" {
  subnet_id      = aws_subnet.primary.id
  route_table_id = aws_route_table.hana.id
}

resource "aws_route_table_association" "secondary" {
  subnet_id      = aws_subnet.secondary.id
  route_table_id = aws_route_table.hana.id
}

# --- Private Route 53 Zone ---

resource "aws_route53_zone" "hana" {
  name = var.private_zone_name
  vpc {
    vpc_id = aws_vpc.hana.id
  }
  tags = { Name = "hana-private-zone" }
}

resource "aws_route53_record" "primary" {
  zone_id = aws_route53_zone.hana.zone_id
  name    = "hana-primary.${var.private_zone_name}"
  type    = "A"
  ttl     = 30
  records = [aws_instance.hana_primary.private_ip]
}

resource "aws_route53_record" "secondary" {
  zone_id = aws_route53_zone.hana.zone_id
  name    = "hana-secondary.${var.private_zone_name}"
  type    = "A"
  ttl     = 60
  records = [aws_instance.hana_secondary.private_ip]
}

# --- IAM for EC2 (S3 access) ---

resource "aws_iam_role" "hana_ec2" {
  name = "hana-ec2-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "hana_s3" {
  name = "hana-s3-read"
  role = aws_iam_role.hana_ec2.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:ListBucket"]
      Resource = [
        "arn:aws:s3:::${var.hana_media_bucket}",
        "arn:aws:s3:::${var.hana_media_bucket}/*"
      ]
    }]
  })
}

resource "aws_iam_instance_profile" "hana_ec2" {
  name = "hana-ec2-profile"
  role = aws_iam_role.hana_ec2.name
}

# --- Security Group ---

resource "aws_security_group" "hana" {
  name        = "hana-sg"
  description = "SSH + internal VPC for HANA HSR"
  vpc_id      = aws_vpc.hana.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "All internal VPC traffic (HSR)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["10.0.0.0/16"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "hana-sg" }
}

# --- EC2 Instances ---

resource "aws_instance" "hana_primary" {
  ami                         = local.sles_sap_ami
  instance_type               = "r5.xlarge"
  key_name                    = var.key_pair_name
  subnet_id                   = aws_subnet.primary.id
  vpc_security_group_ids      = [aws_security_group.hana.id]
  iam_instance_profile        = aws_iam_instance_profile.hana_ec2.name
  associate_public_ip_address = true

  root_block_device {
    volume_type = "gp3"
    volume_size = 50
  }

  tags = { Name = "hana-primary" }
}

resource "aws_instance" "hana_secondary" {
  ami                         = local.sles_sap_ami
  instance_type               = "r5.xlarge"
  key_name                    = var.key_pair_name
  subnet_id                   = aws_subnet.secondary.id
  vpc_security_group_ids      = [aws_security_group.hana.id]
  iam_instance_profile        = aws_iam_instance_profile.hana_ec2.name
  associate_public_ip_address = true

  root_block_device {
    volume_type = "gp3"
    volume_size = 50
  }

  tags = { Name = "hana-secondary" }
}

# --- EBS Volumes ---

resource "aws_ebs_volume" "primary_data" {
  availability_zone = aws_instance.hana_primary.availability_zone
  size              = 50
  type              = "gp3"
  tags              = { Name = "hana-primary-data" }
}

resource "aws_ebs_volume" "primary_log" {
  availability_zone = aws_instance.hana_primary.availability_zone
  size              = 25
  type              = "gp3"
  tags              = { Name = "hana-primary-log" }
}

resource "aws_ebs_volume" "primary_shared" {
  availability_zone = aws_instance.hana_primary.availability_zone
  size              = 50
  type              = "gp3"
  tags              = { Name = "hana-primary-shared" }
}

resource "aws_ebs_volume" "secondary_data" {
  availability_zone = aws_instance.hana_secondary.availability_zone
  size              = 50
  type              = "gp3"
  tags              = { Name = "hana-secondary-data" }
}

resource "aws_ebs_volume" "secondary_log" {
  availability_zone = aws_instance.hana_secondary.availability_zone
  size              = 25
  type              = "gp3"
  tags              = { Name = "hana-secondary-log" }
}

resource "aws_ebs_volume" "secondary_shared" {
  availability_zone = aws_instance.hana_secondary.availability_zone
  size              = 50
  type              = "gp3"
  tags              = { Name = "hana-secondary-shared" }
}

resource "aws_volume_attachment" "primary_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.primary_data.id
  instance_id = aws_instance.hana_primary.id
}

resource "aws_volume_attachment" "primary_log" {
  device_name = "/dev/sdg"
  volume_id   = aws_ebs_volume.primary_log.id
  instance_id = aws_instance.hana_primary.id
}

resource "aws_volume_attachment" "primary_shared" {
  device_name = "/dev/sdh"
  volume_id   = aws_ebs_volume.primary_shared.id
  instance_id = aws_instance.hana_primary.id
}

resource "aws_volume_attachment" "secondary_data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.secondary_data.id
  instance_id = aws_instance.hana_secondary.id
}

resource "aws_volume_attachment" "secondary_log" {
  device_name = "/dev/sdg"
  volume_id   = aws_ebs_volume.secondary_log.id
  instance_id = aws_instance.hana_secondary.id
}

resource "aws_volume_attachment" "secondary_shared" {
  device_name = "/dev/sdh"
  volume_id   = aws_ebs_volume.secondary_shared.id
  instance_id = aws_instance.hana_secondary.id
}

# --- HA Layer: CloudWatch Alarm ---

resource "aws_cloudwatch_metric_alarm" "primary_status" {
  alarm_name          = "hana-primary-status-check-failed"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 2
  metric_name         = "StatusCheckFailed"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Maximum"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_description   = "HANA primary instance status check failed — triggers Lambda failover"

  dimensions = {
    InstanceId = aws_instance.hana_primary.id
  }

  alarm_actions = [aws_sns_topic.failover.arn]
}

# --- HA Layer: SNS ---

resource "aws_sns_topic" "failover" {
  name = "hana-failover"
}

resource "aws_sns_topic_subscription" "lambda" {
  topic_arn = aws_sns_topic.failover.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.failover.arn
}

# --- HA Layer: SSH Key Secret ---

resource "aws_secretsmanager_secret" "ssh_key" {
  name        = "hana-ssh-key"
  description = "SSH private key for HANA EC2 instances — paste contents of HANA_DEP.pem"
}

# --- HA Layer: Lambda IAM ---

resource "aws_iam_role" "lambda" {
  name = "hana-failover-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "hana-failover-lambda"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["route53:ChangeResourceRecordSets", "route53:ListResourceRecordSets"]
        Resource = "arn:aws:route53:::hostedzone/${aws_route53_zone.hana.zone_id}"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.ssh_key.arn
      }
    ]
  })
}

# --- HA Layer: Lambda Function ---

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/package"
  output_path = "${path.module}/lambda_failover.zip"
}

resource "aws_lambda_function" "failover" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "hana-failover"
  role             = aws_iam_role.lambda.arn
  handler          = "failover.handler"
  runtime          = "python3.12"
  timeout          = 600
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = {
      PRIMARY_INSTANCE_ID   = aws_instance.hana_primary.id
      SECONDARY_INSTANCE_ID = aws_instance.hana_secondary.id
      HOSTED_ZONE_ID        = aws_route53_zone.hana.zone_id
      HANA_SID              = var.hana_sid
      HANA_INSTANCE         = var.hana_instance
      HANA_HOSTNAME         = local.hana_hostname
      SSH_KEY_SECRET_ARN    = aws_secretsmanager_secret.ssh_key.arn
    }
  }
}

resource "aws_lambda_permission" "sns" {
  statement_id  = "AllowSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.failover.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.failover.arn
}

# --- HA Layer: EventBridge for re-registration ---

# Fires immediately when primary goes down — triggers failover
resource "aws_cloudwatch_event_rule" "primary_down" {
  name        = "hana-primary-down"
  description = "Triggers Lambda failover the moment primary instance stops or terminates"

  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["EC2 Instance State-change Notification"]
    detail = {
      state       = ["stopped", "terminated", "stopping"]
      instance-id = [aws_instance.hana_primary.id]
    }
  })
}

resource "aws_cloudwatch_event_target" "failover" {
  rule = aws_cloudwatch_event_rule.primary_down.name
  arn  = aws_lambda_function.failover.arn
}

resource "aws_lambda_permission" "eventbridge_failover" {
  statement_id  = "AllowEventBridgeFailover"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.failover.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.primary_down.arn
}

# Fires when a HANA instance comes back — triggers re-registration
resource "aws_cloudwatch_event_rule" "instance_running" {
  name        = "hana-instance-running"
  description = "Re-registers a returning HANA instance as HSR secondary"

  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["EC2 Instance State-change Notification"]
    detail = {
      state       = ["running"]
      instance-id = [aws_instance.hana_primary.id, aws_instance.hana_secondary.id]
    }
  })
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule = aws_cloudwatch_event_rule.instance_running.name
  arn  = aws_lambda_function.failover.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.failover.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.instance_running.arn
}
