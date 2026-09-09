# Terraform Infrastructure for SQS Priority Queues

This directory contains Terraform configuration files to provision AWS SQS FIFO queues and Dead Letter Queues (DLQs) for Stage 2 Priority Tier Queuing & Backpressure.

## Provisions
1. **High Priority Queue**: `llm-requests-high-priority.fifo` (Enterprise Tenant Tier) + DLQ
2. **Standard Queue**: `llm-requests-standard.fifo` (Pro Tenant Tier) + DLQ
3. **Batch Queue**: `llm-requests-batch.fifo` (Free / Default Tenant Tier) + DLQ

## How to Apply

1. Set your AWS environment credentials in `.env` or export them:
   ```bash
   export AWS_ACCESS_KEY_ID="your-access-key-id"
   export AWS_SECRET_ACCESS_KEY="your-secret-access-key"
   export AWS_REGION="us-east-1"
   ```

2. Initialize and apply Terraform:
   ```bash
   cd terraform
   terraform init
   terraform plan
   terraform apply
   ```

3. Copy the output Queue URLs (`sqs_high_priority_url`, `sqs_standard_url`, `sqs_batch_url`) to your `.env` file:
   ```env
   SQS_ENABLED=true
   AWS_REGION=us-east-1
   SQS_HIGH_PRIORITY_URL="https://sqs.us-east-1.amazonaws.com/123456789012/llm-requests-high-priority.fifo"
   SQS_STANDARD_URL="https://sqs.us-east-1.amazonaws.com/123456789012/llm-requests-standard.fifo"
   SQS_BATCH_URL="https://sqs.us-east-1.amazonaws.com/123456789012/llm-requests-batch.fifo"
   ```
