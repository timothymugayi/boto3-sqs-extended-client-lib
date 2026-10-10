# Testing

LocalStack and the local unit tests.

[README](../README.md) · [Configuration](configuration.md) · [IAM](iam.md) · [FIFO and redrive](fifo-and-redrive.md) · [Events](events.md) · [Binary and files](binary-and-files.md) · [Java compatibility](java-compatibility.md) · [Migration](migration.md) · Testing

<a id="localstack"></a>

### LocalStack

Use Docker Compose to run SQS and S3 locally. The init hook creates bucket `sqs-extended-payloads`, queue `sqs-extended-demo`, and FIFO queue `sqs-extended-demo.fifo`.

The compose file pins `localstack/localstack:4.14.0` so it starts **without an account**. LocalStack 2026.03 and later require `LOCALSTACK_AUTH_TOKEN` (see [app.localstack.cloud](https://app.localstack.cloud)); you can override with `LOCALSTACK_IMAGE` and that env var.

```bash
docker compose up -d
```

Wait until `http://127.0.0.1:4566/_localstack/health` responds, then run either the pytest marker or the smoke script. Both talk to `http://127.0.0.1:4566` with dummy credentials. They are skipped or fail if LocalStack is not up.

```bash
pytest -m localstack
python scripts/localstack_smoke.py
docker compose down
```

`pytest -m localstack` runs `tests/test_localstack.py`. `scripts/localstack_smoke.py` is the same checks as a standalone script. Each one checks:

* a small string stays in SQS and comes back as `str`
* a large string is stored in S3 and still comes back as `str`
* small `bytes` come back as `bytes`
* `send_file` / a `pathlib.Path` and a binary file object come back as `bytes`
* a 16 KiB payload with `multipart_threshold=1024` calls S3 `CreateMultipartUpload` and round-trips

Point your own code at the same endpoint:

```python
from pysqs_extended_client import SQSClientExtended, ExtendedClientConfiguration

sqs = SQSClientExtended(
    "test",
    "test",
    "us-east-1",
    config=ExtendedClientConfiguration(
        s3_bucket_name="sqs-extended-payloads",
        endpoint_url="http://127.0.0.1:4566",
    ),
)
queue_url = sqs.get_queue_url(QueueName="sqs-extended-demo")["QueueUrl"]
sqs.send_message(queue_url, "hello from localstack")
```

You can also inject boto3 clients created with `endpoint_url="http://127.0.0.1:4566"`. Injected clients are not wrapped. The default multipart threshold is 8 MB. Lower `multipart_threshold` on `ExtendedClientConfiguration` when you want a small local file to take the multipart path.

<a id="unit-tests"></a>

### Unit tests

Submitted changes should pass the current tests, and be covered by new test cases when adding functionality. From [CONTRIBUTING.md](../CONTRIBUTING.md):

```bash
pip install -r requirements-dev.txt
pytest -q --tb=short -m "not localstack"
```

Each pull request is gated by [GitHub Actions](../.github/workflows/tests.yml) on Python 3.9–3.13. That check must pass before the change can land; pushing a new commit retriggers it.
