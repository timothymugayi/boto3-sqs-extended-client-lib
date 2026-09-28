# Contributing

pysqs-extended-client uses GitHub to manage reviews of pull requests.

* If you have a trivial fix or improvement, go ahead and create a pull request,
  addressing (with `@...`) the maintainer of this repository (see
  [contributors](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/graphs/contributors)).

* If you plan to do something more involved, first discuss your ideas
  by creating an issue. This will avoid unnecessary work and surely give you and
  us a good deal of inspiration.

## Testing

Submitted changes should pass the current tests, and be covered by new test
cases when adding functionality.

Install dev dependencies and run the unit tests:

```bash
pip install -r requirements-dev.txt
pytest -q --tb=short -m "not localstack"
```

LocalStack tests need a running stack:

```bash
docker compose up -d
pytest -q --tb=short -m localstack
docker compose down
```

`scripts/localstack_smoke.py` is the same LocalStack checks as a standalone script.

Each pull request is gated by [GitHub Actions](.github/workflows/tests.yml) on Python 3.9–3.13. That check must pass before the change can land; pushing a new commit retriggers it.

## Style

Code style should follow [PEP 8](https://www.python.org/dev/peps/pep-0008/). This repository does not use tox. Match the existing indentation (tabs) in `pysqs_extended_client/`.
