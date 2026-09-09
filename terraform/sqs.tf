# ==========================================
# 1. Dead Letter Queues (DLQ)
# ==========================================

resource "aws_sqs_queue" "high_priority_dlq" {
  name                        = "llm-requests-high-priority-dlq.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  message_retention_seconds   = 1209600 # 14 days

  tags = {
    Environment = var.environment
    Tier        = "enterprise-dlq"
    ManagedBy   = "Terraform"
  }
}

resource "aws_sqs_queue" "standard_dlq" {
  name                        = "llm-requests-standard-dlq.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  message_retention_seconds   = 1209600

  tags = {
    Environment = var.environment
    Tier        = "pro-dlq"
    ManagedBy   = "Terraform"
  }
}

resource "aws_sqs_queue" "batch_dlq" {
  name                        = "llm-requests-batch-dlq.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  message_retention_seconds   = 1209600

  tags = {
    Environment = var.environment
    Tier        = "batch-dlq"
    ManagedBy   = "Terraform"
  }
}


# ==========================================
# 2. Priority Tenant FIFO Queues
# ==========================================

# High Priority Queue (Enterprise Tier)
resource "aws_sqs_queue" "high_priority" {
  name                        = var.high_priority_queue_name
  fifo_queue                  = true
  content_based_deduplication = true
  visibility_timeout_seconds  = var.visibility_timeout_seconds
  message_retention_seconds   = var.message_retention_seconds

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.high_priority_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Environment = var.environment
    Tier        = "enterprise"
    Priority    = "1-high"
    ManagedBy   = "Terraform"
  }
}

# Standard Priority Queue (Pro Tier)
resource "aws_sqs_queue" "standard" {
  name                        = var.standard_queue_name
  fifo_queue                  = true
  content_based_deduplication = true
  visibility_timeout_seconds  = var.visibility_timeout_seconds
  message_retention_seconds   = var.message_retention_seconds

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.standard_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Environment = var.environment
    Tier        = "pro"
    Priority    = "2-standard"
    ManagedBy   = "Terraform"
  }
}

# Batch Priority Queue (Free / Default Tier)
resource "aws_sqs_queue" "batch" {
  name                        = var.batch_queue_name
  fifo_queue                  = true
  content_based_deduplication = true
  visibility_timeout_seconds  = var.visibility_timeout_seconds
  message_retention_seconds   = var.message_retention_seconds

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.batch_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = {
    Environment = var.environment
    Tier        = "free"
    Priority    = "3-batch"
    ManagedBy   = "Terraform"
  }
}
