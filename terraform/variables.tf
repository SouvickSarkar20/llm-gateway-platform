variable "aws_region" {
  description = "AWS region for SQS queues"
  type        = string
  default     = "us-east-1"
}

variable "aws_endpoint_url" {
  description = "Optional endpoint URL for LocalStack / local SQS testing"
  type        = string
  default     = ""
}

variable "environment" {
  description = "Deployment environment (development, staging, production)"
  type        = string
  default     = "production"
}

variable "high_priority_queue_name" {
  description = "Name of the high priority SQS FIFO queue"
  type        = string
  default     = "llm-requests-high-priority.fifo"
}

variable "standard_queue_name" {
  description = "Name of the standard priority SQS FIFO queue"
  type        = string
  default     = "llm-requests-standard.fifo"
}

variable "batch_queue_name" {
  description = "Name of the batch priority SQS FIFO queue"
  type        = string
  default     = "llm-requests-batch.fifo"
}

variable "visibility_timeout_seconds" {
  description = "Visibility timeout in seconds for processing LLM jobs"
  type        = int
  default     = 120
}

variable "message_retention_seconds" {
  description = "Message retention duration in seconds"
  type        = int
  default     = 86400 # 24 hours
}

variable "max_receive_count" {
  description = "Maximum number of times a message is delivered before being sent to DLQ"
  type        = int
  default     = 3
}
