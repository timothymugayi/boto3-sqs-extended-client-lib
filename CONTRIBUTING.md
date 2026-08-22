# Contributing

pysqs-extended-client uses GitHub to manage reviews of pull requests.

* If you have a trivial fix or improvement, go ahead and create a pull request,
  addressing (with `@...`) the maintainer of this repository (see
  [MAINTAINERS](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/graphs/contributors)).

* If you plan to do something more involved, first discuss your ideas on
  [by creating an issue]. This will avoid unnecessary work and surely give you and
  us a good deal of inspiration.

## Testing

Submitted changes should pass the current tests, and be covered by new test
cases when adding functionality.

* Run the tests locally with ``pytest`` (see ``requirements-dev.txt``). LocalStack tests need ``docker compose up -d`` and ``pytest -m localstack``.

* Each pull request is gated by [GitHub Actions](.github/workflows/tests.yml). That check must pass before the change can land; pushing a new commit retriggers it.

## Style

* Code style should follow [PEP 8] generally, and can be checked by running:
  ``tox -e flake8``.

* Import statements can be automatically formatted using [isort].

[isort]: https://pypi.org/project/isort/
[PEP 8]: https://www.python.org/dev/peps/pep-0008/
[tox]: https://tox.readthedocs.io/en/latest/
