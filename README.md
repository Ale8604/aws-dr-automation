# DRP Automation — Disaster Recovery Failover

## 1. Project Overview

This repository contains an AWS Disaster Recovery (DR) automation implementation that orchestrates failover from the Primary region (us-east-1) to a DR region (us-west-2). The system is implemented with AWS Step Functions, AWS Lambda, and Terraform. A manual Systems Manager Automation Runbook (the "Red Button") triggers the DR workflow.

Target audience: DevOps engineers and SREs responsible for running and validating DR failovers.


## 2. Architecture Overview

The implementation uses the following control and data-plane components:

- Control plane: AWS Systems Manager Automation (manual trigger), AWS Step Functions (state machine `drp-workflow`), and Lambda functions invoked by the state machine.
- Data plane: Amazon RDS (Postgres), EC2 (Auto Scaling Group / Launch Template), ALB (Target Group) and Route53 for DNS switching.

Mermaid diagram (architecture):

```mermaid
flowchart LR
  subgraph Primary Region (us-east-1)
    direction TB
    P_ALB[ALB (Primary)]
    P_ASG[Auto Scaling Group (Primary)]
    P_RDS[RDS PostgreSQL (Primary)]
    P_ASG --> P_ALB
    P_RDS --- P_ASG
  end

  subgraph DR Region (us-west-2)
    direction TB
    DR_RDS[RDS Read Replica (DR)]
    DR_ASG[Auto Scaling Group (DR)]
    DR_ALB[ALB (DR)]
    DR_ASG --> DR_ALB
    DR_RDS --- DR_ALB
  end

  ControlPlane[Control Plane]
  ControlPlane --> SSM[SSM Automation: DRP-Runbook-RedButton]
  ControlPlane --> SF[Step Functions: drp-workflow]
  SF --> L1[Lambda: check_rds_replica]
  SF --> L2[Lambda: validate_ami_tag]
  SF --> L3[Lambda: promote_rds_replica]
  SF --> L4[Lambda: deploy_ec2_instances]
  SF --> L5[Lambda: check_targetgroup_health]
  SF --> L6[Lambda: update_route53_failover]

  SF --> Route53[Route53]
  Route53 --> Clients[Clients -> DR ALB]

  classDef control fill:#f4f7ff,stroke:#3b82f6;
  class ControlPlane control
```


## 3. Disaster Recovery Strategy

- The DR process is executed manually by an operator using an SSM Automation runbook called `DRP-Runbook-RedButton`.
- The runbook starts the Step Functions state machine `drp-workflow` (StartExecution). The Step Functions workflow orchestrates a sequence of Lambda tasks to validate, promote, deploy, health-check, and update DNS to cut traffic to DR.
- Failback is manual: this repository includes a documented runbook (FAILBACK.md) that explains how to create a cross-region replica and promote it back in the primary region.


## 4. Infrastructure Components

Provisioned resources (via Terraform):

- Lambda functions (Python): `validate_ami_tag`, `check_rds_replica`, `promote_rds_replica`, `deploy_ec2_instances`, `check_targetgroup_health`, `update_route53_failover`.
- IAM roles and inline policies for the Lambdas.
- AWS Step Functions state machine `drp-workflow`.
- AWS Systems Manager Automation document `DRP-Runbook-RedButton` and IAM role/policy for the runbook.
- Terraform packaging using `archive_file` to zip Lambda source files.


## 5. Step Functions Workflow

State machine name: `drp-workflow` (STANDARD)

Orchestration sequence (exactly as implemented):

1. Validate AMI (`validate_ami_tag`)
2. Validate RDS read replica (`check_rds_replica`)
3. Promote RDS read replica (`promote_rds_replica`)
4. Deploy EC2 instances in DR (`deploy_ec2_instances`)
5. Validate instance initialization / health (`check_targetgroup_health`)
6. Update Route53 to point to DR ALB (`update_route53_failover`)

Each step is invoked as a Lambda Task integration. Specific retry behavior and ResultPath handling are configured in the state machine definition in `stepfuntions/drp-workflow.json`.


## 6. Lambda Functions Description

Below are the Lambdas with their purpose, AWS services they call, and expected input/output within the workflow.

- `validate_ami_tag`
  - Purpose: Find the most recent AMI in the DR region with tag `DR_READY=true` and ensure it is available.
  - AWS services: Amazon EC2 `describe_images`.
  - Expected input: optional `Owner` or other search params; default region is `us-west-2`.
  - Output: `{ "amiExists": true/false, "amiId": "ami-...", "creationDate": "..." }` or raises `AMI_NOT_FOUND` error for the workflow to stop.

- `check_rds_replica`
  - Purpose: Verify that the RDS read replica (in DR region) is `available` and that replication lag is acceptable.
  - AWS services: Amazon RDS `describe_db_instances`.
  - Expected input: `ReplicaIdentifier` or `DBInstanceIdentifier`.
  - Behavior: returns replica status; raises `ReplicaNotReady` to trigger Step Functions retry if not `available`; raises `ReplicaLagTooHigh` when ReplicaLag > 30 seconds.
  - Output: `{ "replicaAvailable": true/false, "replicaStatus": "available|...", "dbInstanceIdentifier": "..." }

- `promote_rds_replica`
  - Purpose: Promote a DR read replica to primary (existing behavior). Additionally, supports a manual `create_replica` mode to create a read replica (for manual failback preparations).
  - AWS services: Amazon RDS `promote_read_replica`, `create_db_instance_read_replica`, `describe_db_instances`.
  - Expected input (promotion): `DBInstanceIdentifier` (replica id), optional `Region`.
  - Additional input (create replica): `mode: "create_replica"`, optional `SourceDBInstanceIdentifier`, optional `NewDBInstanceIdentifier`.
  - Output: On success returns `status: success` and `instance` details for the promoted or created instance. On errors returns `status: error` with code/message.

- `deploy_ec2_instances`
  - Purpose: Create a Launch Template and Auto Scaling Group (ASG) in the DR region using the validated AMI and desired instance count (workflow uses count=4).
  - AWS services: EC2 (`create_launch_template`, `describe_instances`), Auto Scaling (`create_auto_scaling_group`, `describe_auto_scaling_groups`).
  - Expected input: `AmiId`, `InstanceCount` (default 4), `InstanceType`, `SecurityGroupIds`, `SubnetIds` (or `SubnetId`), `KeyName`, `TargetGroupArn`, `Name` (tag).
  - Output: `{ "asgName": "...", "launchTemplateId": "...", "desiredCapacity": N, "instanceIds": ["i-...", ...] }` when instances are observed `InService` in the ASG.
  - Note: The Lambda waits and polls the ASG for instances to reach `InService` (configurable via environment variables in the Lambda).

- `check_targetgroup_health`
  - Purpose: Validate that target group health for the provided instance IDs is `healthy` in the ALB target group.
  - AWS services: ELBv2 `describe_target_health`.
  - Expected input: `InstanceIds` (list) and `TargetGroupArn`.
  - Output: `{ "allHealthy": true/false, "healthy": [...], "unhealthy": [...], "details": {...} }`. Raises `UnhealthyTargets` to trigger retries if not all healthy.

- `update_route53_failover`
  - Purpose: Perform a Route53 `UPSERT` for the application's DNS record to point to the DR ALB (alias) and wait for the Route53 change to be submitted.
  - AWS services: ELBv2 `describe_load_balancers` (to discover ALB DNS/zone), Route53 `ChangeResourceRecordSets`.
  - Expected input: `HostedZoneId`, `RecordName`, and either `TargetLoadBalancerDnsName` or `LoadBalancerArn`.
  - Output: returns changeInfo from Route53 and `recordName` on success.


## 7. Manual DR Trigger (Red Button)

- SSM Automation document name: `DRP-Runbook-RedButton`.
- Purpose: Provide a single-button manual operation that starts the `drp-workflow` Step Functions state machine.
- How it works: the automation document uses the `aws:executeAwsApi` action to call `states:StartExecution` against the Step Functions state machine `drp-workflow` and accepts an `ExecutionInput` parameter for the JSON payload.

How an engineer triggers DR manually:

1. In the AWS Console navigate to Systems Manager > Automation.
2. Find `DRP-Runbook-RedButton` and start an automation execution.
3. Provide a JSON `ExecutionInput` matching the Step Functions input structure (or use `{}` for defaults). The runbook assumes a role that has `states:StartExecution` permission on the `drp-workflow` state machine.

CLI example (start automation execution):

```bash
aws ssm start-automation-execution \
  --document-name "DRP-Runbook-RedButton" \
  --parameters ExecutionInput='{"DBInstanceIdentifier":"replica-id","TargetGroupArn":"arn:..."}'
```

Or invoke the Step Functions state machine directly (if you prefer):

```bash
aws stepfunctions start-execution \
  --state-machine-arn <state-machine-arn> \
  --input '{"DBInstanceIdentifier":"replica-id","TargetGroupArn":"arn:..."}'
```


## 8. Failover Execution Flow (Detailed)

1. Validate AMI: `validate_ami_tag` searches for `DR_READY=true` AMIs in `us-west-2` and enforces AMI state `available`. If missing, the workflow stops.
2. Validate RDS read replica: `check_rds_replica` verifies the replica is `available` and checks replication lag (fails if lag exceeds threshold).
3. Promote RDS read replica: `promote_rds_replica` promotes the replica to primary (the Lambda now also supports a manual `create_replica` mode, but promotion is the default path in the workflow).
4. Deploy EC2 instances: `deploy_ec2_instances` creates a Launch Template + ASG configured for the validated AMI and desired instance count (workflow uses 4 instances). The Lambda returns the created `instanceIds` after instances reach `InService`.
5. Health check: `check_targetgroup_health` confirms all instances are `healthy` in the ALB target group (with retries configured in the state machine).
6. Update DNS: `update_route53_failover` performs a Route53 `UPSERT` to point the application record to the DR ALB (Alias) and returns the Route53 change info.

When all steps succeed the Step Function completes and traffic is directed to DR.


## 9. Failback Procedure (Manual)

Failback is manual and documented in `FAILBACK.md`. The typical manual failback process is:

1. Create a cross-region read replica from the active DR primary to the original primary region (us-east-1) using `create-db-instance-read-replica` or restore/replicate as appropriate.
2. Wait for the replica in us-east-1 to become `available` and verify replication / application data.
3. Promote the replica in us-east-1 using `promote-read-replica`.
4. Update Route53 to point traffic back to the primary ALB in us-east-1 using the `update_route53_failover` Lambda or console.

Refer to `FAILBACK.md` for step-by-step CLI commands and validations.


## 10. Deployment (Terraform)

The repository includes a `terraform/` configuration that packages Lambdas via `archive_file`, creates IAM roles/policies, deploys Lambda functions, creates the Step Functions state machine, and adds the SSM Automation document.

Typical workflow (local engineer):

```bash
cd terraform
terraform init
terraform plan -var 'db_instance_identifier=primary-db' -var 'hosted_zone_id=ZAAAAAA' -var 'target_group_arn=arn:aws:elasticloadbalancing:us-west-2:123456789012:targetgroup/example/abcdef'
terraform apply -var 'db_instance_identifier=primary-db' -var 'hosted_zone_id=ZAAAAAA' -var 'target_group_arn=arn:aws:elasticloadbalancing:us-west-2:123456789012:targetgroup/example/abcdef'
```

Outputs available in Terraform:
- `lambda_arns` — ARNs for all deployed Lambda functions
- `state_machine_arn` — ARN for the `drp-workflow` state machine

Note: Terraform requires AWS credentials available to the CLI (environment variables, profile, or instance role) to plan and apply.


## 11. Operational Considerations

- Route53 TTL: Use a low TTL for the application record (e.g., 60s) to speed cutover/recovery.
- IAM: Lambdas require least-privilege IAM policies to access RDS, EC2, ELBv2, AutoScaling and Route53. The runbook role requires `states:StartExecution` on the `drp-workflow` state machine.
- Idempotency: Lambdas try to avoid duplicate resources (e.g., using deterministic naming when creating replicas or tags). The workflow is safe to retry for transient failures.
- Manual control: The SSM runbook provides deliberate manual control — do not automate runbook execution without approval.


## 12. Monitoring and Logging

- CloudWatch Logs: Each Lambda writes logs to CloudWatch — review logs for debugging failures in each workflow step.
- Step Functions: Execution history and state machine traces are available in the Step Functions console. Use the execution view to inspect input/output at each step.
- Alarms & Notifications: Integrate CloudWatch Alarms and SNS for critical failures and on-call alerts as needed.


---

This README describes the current implementation present in this repository. For procedural details and CLI command examples for failback, see `FAILBACK.md` in the repository.
