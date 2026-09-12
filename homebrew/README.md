# Homebrew tap

`brew install tamaratran/tap/voice-conductor` needs a tap repository -
Homebrew reads formulae from a repo named `homebrew-tap`, not from the app
repo. One-time setup:

1. Create a public GitHub repo named `tamaratran/homebrew-tap`.
2. Copy `voice-conductor.rb` from this directory into a `Formula/` folder
   there.

Then anyone can:

```bash
brew install --HEAD tamaratran/tap/voice-conductor
conduct
```

The formula is head-only (it installs from the current `main`) because
the app has no versioned releases yet. Once tags exist, add a `url`/`sha256`
pointing at the release tarball so plain `brew install` works too.

The `curl | bash` installer in the repo root (`install.sh`) does the same
job without Homebrew and is the primary path.
