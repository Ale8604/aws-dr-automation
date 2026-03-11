/* restored monolithic Terraform configuration */
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.0.0"
    }
  }
}

provider "aws" {
  region = "us-west-2"
}

data "aws_caller_identity" "current" {}

variable "db_instance_identifier" {
  type        = string
  description = "Primary DB instance identifier (used by Lambdas/workflow)"
}

variable "hosted_zone_id" {
  type        = string
  description = "Route53 Hosted Zone ID"
}

variable "target_group_arn" {
  type        = string
  description = "Target Group ARN for health checks / ALB"
}

locals {
  lambda_sources = [
    "validate_ami_tag",
    "check_rds_replica",
    "promote_rds_replica",
    "deploy_ec2_instances",
    "check_targetgroup_health",
    "update_route53_failover",
  ]
}

data "archive_file" "validate_ami_tag" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/validate_ami_tag.py"
  output_path = "${path.module}/../lambdas/validate_ami_tag.zip"
}

data "archive_file" "check_rds_replica" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/check_rds_replica.py"
  output_path = "${path.module}/../lambdas/check_rds_replica.zip"
}

data "archive_file" "promote_rds_replica" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/promote_rds_replica.py"
  output_path = "${path.module}/../lambdas/promote_rds_replica.zip"
}

data "archive_file" "deploy_ec2_instances" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/deploy_ec2_instances.py"
  output_path = "${path.module}/../lambdas/deploy_ec2_instances.zip"
}

data "archive_file" "check_targetgroup_health" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/check_targetgroup_health.py"
  output_path = "${path.module}/../lambdas/check_targetgroup_health.zip"
}

data "archive_file" "update_route53_failover" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/update_route53_failover.py"
  output_path = "${path.module}/../lambdas/update_route53_failover.zip"
}

resource "aws_iam_role" "lambda_exec" {
  name = "drp-lambda-exec-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "drp-lambda-inline-policy"
  role = aws_iam_role.lambda_exec.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid = "CloudWatchLogs",
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ],
        Effect   = "Allow",
        Resource = "arn:aws:logs:us-west-2:${data.aws_caller_identity.current.account_id}:*"
      },
      {
        Sid = "EC2Actions",
        # Some EC2 actions (RunInstances, CreateLaunchTemplate) require resource "*"; describe actions limited to account
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeTags",
          "ec2:CreateTags"
        ],
        Effect = "Allow",
        Resource = [
          "arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:instance/*",
          "arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:volume/*",
          "arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:network-interface/*"
        ]
      },
      {
        Sid      = "EC2CreateActions",
        Action   = ["ec2:RunInstances", "ec2:CreateLaunchTemplate"],
        Effect   = "Allow",
        Resource = "*"
      },
      {
        Sid    = "RDSActions",
        Action = ["rds:DescribeDBInstances", "rds:PromoteReadReplica", "rds:CreateDBInstanceReadReplica", "rds:DescribeDBLogFiles"],
        Effect = "Allow",
        Resource = [
          "arn:aws:rds:us-west-2:${data.aws_caller_identity.current.account_id}:db:${var.db_instance_identifier}",
          "arn:aws:rds:us-west-2:${data.aws_caller_identity.current.account_id}:db:*"
        ]
      },
      {
        Sid      = "Route53Actions",
        Action   = ["route53:ChangeResourceRecordSets", "route53:GetHostedZone", "route53:ListResourceRecordSets"],
        Effect   = "Allow",
        Resource = "arn:aws:route53:::hostedzone/${var.hosted_zone_id}"
      }
      ,
      {
        Sid    = "ELBReadPermissions",
        Effect = "Allow",
        Action = [
          "elasticloadbalancing:DescribeTargetHealth",
          "elasticloadbalancing:DescribeTargetGroups",
          "elasticloadbalancing:DescribeLoadBalancers"
        ],
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic_exec" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "validate_ami" {
  filename         = data.archive_file.validate_ami_tag.output_path
  function_name    = "ValidateAMIFunction"
  handler          = "validate_ami_tag.lambda_handler"
  runtime          = "python3.9"
  role             = aws_iam_role.lambda_exec.arn
  source_code_hash = data.archive_file.validate_ami_tag.output_base64sha256
  publish          = true
}

resource "aws_lambda_function" "check_rds_replica" {
  filename         = data.archive_file.check_rds_replica.output_path
  function_name    = "CheckRDSReplicaFunction"
  handler          = "check_rds_replica.lambda_handler"
  runtime          = "python3.9"
  role             = aws_iam_role.lambda_exec.arn
  source_code_hash = data.archive_file.check_rds_replica.output_base64sha256
  publish          = true
}

resource "aws_lambda_function" "promote_rds_replica" {
  filename         = data.archive_file.promote_rds_replica.output_path
  function_name    = "PromoteRDSReplicaFunction"
  handler          = "promote_rds_replica.lambda_handler"
  runtime          = "python3.9"
  role             = aws_iam_role.lambda_exec.arn
  source_code_hash = data.archive_file.promote_rds_replica.output_base64sha256
  publish          = true
}

resource "aws_lambda_function" "deploy_ec2_instances" {
  filename         = data.archive_file.deploy_ec2_instances.output_path
  function_name    = "DeployEC2InstancesFunction"
  handler          = "deploy_ec2_instances.lambda_handler"
  runtime          = "python3.9"
  role             = aws_iam_role.lambda_exec.arn
  source_code_hash = data.archive_file.deploy_ec2_instances.output_base64sha256
  publish          = true
}

resource "aws_lambda_function" "check_targetgroup_health" {
  filename         = data.archive_file.check_targetgroup_health.output_path
  function_name    = "CheckTargetGroupHealthFunction"
  handler          = "check_targetgroup_health.lambda_handler"
  runtime          = "python3.9"
  role             = aws_iam_role.lambda_exec.arn
  source_code_hash = data.archive_file.check_targetgroup_health.output_base64sha256
  publish          = true
}

# IAM role for SSM Automation runbook to start the Step Functions execution
resource "aws_iam_role" "ssm_automation_role" {
  name = "drp-ssm-automation-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ssm.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "ssm_automation_policy" {
  name = "drp-ssm-automation-policy"
  role = aws_iam_role.ssm_automation_role.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid      = "AllowStartStepFunction",
        Effect   = "Allow",
        Action   = ["states:StartExecution"],
        Resource = aws_sfn_state_machine.drp_workflow.arn
      }
    ]
  })
}

# SSM Automation document (DR Automation Red Button)
resource "aws_ssm_document" "drp_runbook" {
  name          = "DRP-Runbook-RedButton"
  document_type = "Automation"

  content = <<DOC
{
  "schemaVersion": "0.3",
  "description": "Manual trigger to start the DRP Step Functions workflow",
  "parameters": {
    "AutomationAssumeRole": {
      "type": "String",
      "description": "Role ARN to assume for the runbook",
      "default": "${aws_iam_role.ssm_automation_role.arn}"
    },
    "ExecutionInput": {
      "type": "String",
      "description": "JSON input to pass to the state machine",
      "default": "{}"
    }
  },
  "mainSteps": [
    {
      "name": "startStepFunction",
      "action": "aws:executeAwsApi",
      "inputs": {
        "Service": "states",
        "Api": "StartExecution",
        "AssumeRole": "{{AutomationAssumeRole}}",
        "Parameters": {
          "stateMachineArn": "${aws_sfn_state_machine.drp_workflow.arn}",
          "input": "{{ExecutionInput}}"
        }
      }
    }
  ]
}
DOC
}

resource "aws_lambda_function" "update_route53_failover" {
  filename         = data.archive_file.update_route53_failover.output_path
  function_name    = "UpdateRoute53FailoverFunction"
  handler          = "update_route53_failover.lambda_handler"
  runtime          = "python3.9"
  role             = aws_iam_role.lambda_exec.arn
  source_code_hash = data.archive_file.update_route53_failover.output_base64sha256
  publish          = true
}

resource "aws_iam_role" "stepfunctions_role" {
  name = "drp-stepfunctions-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "states.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "stepfunctions_invoke" {
  name = "drp-stepfunctions-invoke-lambda"
  role = aws_iam_role.stepfunctions_role.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        Action = ["lambda:InvokeFunction"],
        Resource = [
          aws_lambda_function.validate_ami.arn,
          aws_lambda_function.check_rds_replica.arn,
          aws_lambda_function.promote_rds_replica.arn,
          aws_lambda_function.deploy_ec2_instances.arn,
          aws_lambda_function.check_targetgroup_health.arn,
          aws_lambda_function.update_route53_failover.arn
        ]
      }
    ]
  })
}

// Add autoscaling permissions to lambda role (inline policy patch)
resource "aws_iam_role_policy" "lambda_autoscaling_policy" {
  name = "drp-lambda-autoscaling-policy"
  role = aws_iam_role.lambda_exec.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid    = "ASGActions",
        Effect = "Allow",
        Action = [
          "autoscaling:CreateAutoScalingGroup",
          "autoscaling:UpdateAutoScalingGroup",
          "autoscaling:DescribeAutoScalingGroups",
          "autoscaling:DescribeAutoScalingInstances",
          "autoscaling:CreateOrUpdateTags",
          "autoscaling:TerminateInstanceInAutoScalingGroup"
        ],
        Resource = [
          "arn:aws:autoscaling:us-west-2:${data.aws_caller_identity.current.account_id}:autoScalingGroup:*:autoScalingGroupName/*",
          "arn:aws:autoscaling:us-west-2:${data.aws_caller_identity.current.account_id}:launchConfiguration:*"
        ]
      }
    ]
  })
}

resource "aws_sfn_state_machine" "drp_workflow" {
  name     = "drp-workflow"
  role_arn = aws_iam_role.stepfunctions_role.arn

  definition = templatefile("${path.module}/../stepfuntions/drp-workflow.json", {
    ValidateAMIFunction            = aws_lambda_function.validate_ami.arn,
    CheckRDSReplicaFunction        = aws_lambda_function.check_rds_replica.arn,
    PromoteRDSReplicaFunction      = aws_lambda_function.promote_rds_replica.arn,
    DeployEC2InstancesFunction     = aws_lambda_function.deploy_ec2_instances.arn,
    CheckTargetGroupHealthFunction = aws_lambda_function.check_targetgroup_health.arn,
    UpdateRoute53FailoverFunction  = aws_lambda_function.update_route53_failover.arn
  })

  type = "STANDARD"
}

output "lambda_arns" {
  value = {
    validate_ami             = aws_lambda_function.validate_ami.arn
    check_rds_replica        = aws_lambda_function.check_rds_replica.arn
    promote_rds_replica      = aws_lambda_function.promote_rds_replica.arn
    deploy_ec2_instances     = aws_lambda_function.deploy_ec2_instances.arn
    check_targetgroup_health = aws_lambda_function.check_targetgroup_health.arn
    update_route53_failover  = aws_lambda_function.update_route53_failover.arn
  }
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.drp_workflow.arn
}
