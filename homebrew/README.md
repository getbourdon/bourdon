# Homebrew formula

`Formula/bourdon.rb` lets Mac users install Bourdon via Homebrew without thinking about Python versioning.

## For users

```bash
brew tap getbourdon/bourdon https://github.com/getbourdon/bourdon
brew install getbourdon/bourdon/bourdon
```

The non-standard tap URL (passing the repo as the second arg to `brew tap`) is needed because the formula lives in the main `getbourdon/bourdon` repo rather than a dedicated `getbourdon/homebrew-bourdon` tap. Once a separate tap repo exists, this collapses to `brew tap getbourdon/bourdon && brew install bourdon`.

Verify install:

```bash
bourdon --help
bourdon setup
```

`rsync` and `python@3.12` are declared deps; Homebrew installs them automatically.

## For maintainers

The formula pins exact upstream versions + SHA256s. When releasing a new Bourdon version, refresh both the main package URL and any changed resource dependency.

### Refresh the main package SHA

```bash
VERSION=v0.6.0  # change to new tag
curl -sL "https://github.com/getbourdon/bourdon/archive/refs/tags/${VERSION}.tar.gz" | shasum -a 256
```

Paste the hex (first column) into `Formula/bourdon.rb`'s `sha256 "..."` line under the main `url`. Bump the `url` to point at the new tag.

### Refresh PyYAML

```bash
curl -s https://pypi.org/pypi/PyYAML/json | python3 -c "
import sys, json
d = json.load(sys.stdin)
sdist = [u for u in d['urls'] if u['packagetype'] == 'sdist'][0]
print('url    ', sdist['url'])
print('sha256 ', sdist['digests']['sha256'])
"
```

Update the `resource \"pyyaml\"` block accordingly.

### Local test

```bash
brew install --build-from-source ./Formula/bourdon.rb
brew test bourdon
```

The `test do` block runs `bourdon --help` (must list `setup`, `demo`, `doctor`, `sync`) plus `bourdon demo --no-keep` (must surface `DemoProject` in the synthetic walkthrough). Both are tight assertions on advertised UX — if either fails, the formula's claims have drifted from the code.

### Submitting to homebrew-core (future)

Pre-alpha software (BSL 1.1, < 1.0.0 version, recent rename) does not yet qualify for `homebrew/core` per their notability criteria. Re-evaluate after v1.0.0 + sustained user count.

Until then, the non-standard tap path documented above is the supported route.
