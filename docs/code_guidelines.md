# Code Style

- Code line length: 120
- Use double quotes as default (don't mix and match for simple quoting, checked with pylint).
- Configuration:
    - `.pylint.rc` for pylint
    - `setup.cfg` for flake8 and isort

## Tooling

Install all code linting tools:

```shell
pip3 install -r test-requirements.txt
```

### Verify

The following tests are run as GitHub action for each push on the main branch and for pull requests.
They can also be run anytime on a local developer machine:

```shell
python -m pylint intg-manager
python -m flake8 intg-manager --count --show-source --statistics
python -m isort intg-manager/. --check --verbose 
python -m black intg-manager --check --verbose --line-length 120
```

Linting integration in PyCharm/IntelliJ IDEA:
1. Install plugin [Pylint](https://plugins.jetbrains.com/plugin/11084-pylint)
2. Open Pylint window and run a scan: `Check Module` or `Check Current File`

### Format Code
```shell
python -m black intg-manager --line-length 120
```

PyCharm/IntelliJ IDEA integration:
1. Go to `Preferences or Settings -> Tools -> Black`
2. Configure:
- Python interpreter
- Use Black formatter: `On code reformat` & optionally `On save`
- Arguments: `--line-length 120`

### Sort Imports

```shell
python -m isort intg-manager/.
```
