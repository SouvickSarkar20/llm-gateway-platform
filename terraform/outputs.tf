output "sqs_high_priority_url" {
  description = "URL of the High Priority SQS FIFO Queue"
  value       = aws_sqs_queue.high_priority.id
}

output "sqs_high_priority_arn" {
  description = "ARN of the High Priority SQS FIFO Queue"
  value       = aws_sqs_queue.high_priority.arn
}

output "sqs_standard_url" {
  description = "URL of the Standard SQS FIFO Queue"
  value       = aws_sqs_queue.standard.id
}

output "sqs_standard_arn" {
  description = "ARN of the Standard SQS FIFO Queue"
  value       = aws_sqs_queue.standard.arn
}

output "sqs_batch_url" {
  description = "URL of the Batch SQS FIFO Queue"
  value       = aws_sqs_queue.batch.id
}

output "sqs_batch_arn" {
  description = "ARN of the Batch SQS FIFO Queue"
  value       = aws_sqs_queue.batch.arn
}
