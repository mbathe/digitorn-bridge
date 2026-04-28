# Queue Module - Action Reference

## create_queue

Create or ensure a named queue exists.

**Parameters:**
- `name` (required): Queue name (alphanumeric + hyphens).
- `config`: Backend-specific config (e.g. `visibility_timeout`, `max_retries`).

## publish

Publish a message to a queue.

**Parameters:**
- `queue` (required): Target queue name.
- `message` (required): Message body (any JSON-serializable value).
- `priority`: Priority 0 (highest) to 9 (lowest) (default: 5).
- `delay_seconds`: Hold message for N seconds before delivery (default: 0, max: 900).
- `headers`: Message headers/metadata.

## subscribe

Start consuming messages from a queue in the background.

**Parameters:**
- `queue` (required): Queue to subscribe to.
- `batch_size`: Messages per batch (default: 1, max: 100).
- `filter_headers`: Only receive messages whose headers match these key-value pairs.

## unsubscribe

Stop consuming messages from a subscription.

**Parameters:**
- `subscription_id` (required): Subscription ID returned by subscribe.

## receive

Pull messages from a queue (poll mode).

**Parameters:**
- `queue` (required): Queue to receive from.
- `timeout`: Wait up to N seconds for messages (default: 5, max: 30).
- `batch_size`: Max messages to receive (default: 1, max: 10).
- `ack_mode`: `auto` = auto-ack on receive, `manual` = must call ack() (default: `manual`).

## ack

Acknowledge processed messages (removes them from the queue).

**Parameters:**
- `queue` (required): Queue name.
- `message_ids` (required): List of `ack_id` values from received messages.

## nack

Negative-acknowledge messages (retry or send to dead-letter).

**Parameters:**
- `queue` (required): Queue name.
- `message_ids` (required): List of `ack_id` values to reject.
- `requeue`: True = retry later, False = send to dead-letter queue (default: true).

## peek

Preview messages without consuming them.

**Parameters:**
- `queue` (required): Queue to peek into.
- `count`: Number of messages to preview (default: 5, max: 100).

## queue_stats

Get queue depth, consumer count, and throughput statistics.

**Parameters:**
- `queue` (required): Queue name.

## list_queues

List all known queues.

No parameters.

## delete_queue

Delete a queue and all its messages permanently.

**Parameters:**
- `queue` (required): Queue to delete.

## purge

Remove all messages from a queue without deleting the queue.

**Parameters:**
- `queue` (required): Queue to purge.

## dead_letter

View messages in the dead-letter queue (failed after max retries).

**Parameters:**
- `queue` (required): Queue whose dead-letter messages to view.
- `count`: Number of dead-letter messages to return (default: 10, max: 100).
