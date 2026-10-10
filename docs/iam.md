# IAM

Producer and consumer policies, and cross-account access to the payload bucket.

[README](../README.md) · [Configuration](configuration.md) · IAM · [FIFO and redrive](fifo-and-redrive.md) · [Events](events.md) · [Binary and files](binary-and-files.md) · [Java compatibility](java-compatibility.md) · [Migration](migration.md) · [Testing](testing.md)

<a id="iam"></a>

### IAM

Prefer one role for producers and one for consumers. Scope S3 to the payload prefix. Add the KMS statements only when `server_side_encryption` is a KMS strategy. `sqs:GetQueueAttributes` is required in 0.5.0 for FIFO send, batch send, and receive. A least-privilege FIFO producer that previously only allowed `sqs:SendMessage` must add it or those calls fail closed.

Producer (send). `s3:DeleteObject` is what delete-on-rejection uses. Without it, a definite SQS rejection cannot remove the object that was just uploaded.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sqs:SendMessage", "sqs:GetQueueAttributes"],
      "Resource": "arn:aws:sqs:us-east-1:123456789012:my-queue"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::my-payload-bucket/payloads/*"
    },
    {
      "Effect": "Allow",
      "Action": ["kms:GenerateDataKey", "kms:Encrypt"],
      "Resource": "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
    }
  ]
}
```

Consumer (receive, delete, visibility). `s3:DeleteObject` is required only when `cleanup_s3_payload` is true. `s3:ListBucket` has to be on the bucket itself (`arn:aws:s3:::BUCKET`), with no prefix condition. `s3:prefix` only applies to `ListBucket` requests, so it does not change `GetObject`. Without bucket-level `ListBucket`, a missing object returns 403 instead of 404, `message_left` never fires, and the message dead-letters silently.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"],
      "Resource": "arn:aws:sqs:us-east-1:123456789012:my-queue"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::my-payload-bucket/payloads/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::my-payload-bucket"
    },
    {
      "Effect": "Allow",
      "Action": ["kms:Decrypt"],
      "Resource": "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
    }
  ]
}
```

### Cross-account S3 payloads

Uploads default to the bucket's Object Ownership settings (no canned ACL). Prefer a **bucket policy** that grants the consuming account `s3:GetObject` (and `s3:DeleteObject` if `cleanup_s3_payload` is on).

If you still need a canned ACL, set `s3_canned_acl` (for example `"bucket-owner-read"` or `"bucket-owner-full-control"`). That is passed as `ExtraArgs={"ACL": ...}` on `upload_fileobj`. Buckets with Object Ownership **BucketOwnerEnforced** reject ACLs; leave `s3_canned_acl` unset in that case.

FIFO `sqs:GetQueueAttributes` is covered in [Redrive policy](fifo-and-redrive.md#redrive-policy).
