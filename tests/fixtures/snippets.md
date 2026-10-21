# Sample for the snippet linter

A complete workflow is linted as it is:

```yaml
name: CI
on: push
permissions:
  contents: read
jobs:
  python:
    uses: owner/repo/.github/workflows/python-ci.yml@0123456789abcdef0123456789abcdef01234567 # v1.1.0
```

A job fragment is linted under a minimal header:

```yaml
jobs:
  python:
    uses: owner/repo/.github/workflows/python-ci.yml@0123456789abcdef0123456789abcdef01234567 # v1.1.0
```

A fragment with its own permissions gets only `on:`:

```yml
permissions:
  contents: read
jobs:
  python:
    uses: owner/repo/.github/workflows/python-ci.yml@0123456789abcdef0123456789abcdef01234567 # v1.1.0
```

The major tag accepts the finding of zizmor's unpinned-uses audit:

<!-- lint-snippets: accept unpinned-uses -->
```yaml
jobs:
  python:
    uses: owner/repo/.github/workflows/python-ci.yml@v1
```

A with: fragment, a block in another language and a block inside another block are
not linted:

```yaml
    with:
      python-versions: '["3.13"]'
```

```sh
jobs:
```

~~~markdown
```yaml
jobs:
```
~~~

- A block in a list item loses the item's indentation, and without a marker its
  unpinned reference is a finding:

  ```yaml
  jobs:
    python:
      uses: owner/repo/.github/workflows/python-ci.yml@v1
  ```
