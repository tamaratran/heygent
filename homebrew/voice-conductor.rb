# The Homebrew formula for the Voice Conductor. It lives here so the tap
# repo can copy it verbatim; Homebrew reads formulae from a tap, not from
# the app repo. See homebrew/README.md for setting the tap up.
class VoiceConductor < Formula
  desc "Hold Fn, talk, and manage many Claude Code sessions by voice"
  homepage "https://github.com/tamaratran/voice-agent"
  head "https://github.com/tamaratran/voice-agent.git", branch: "master"

  depends_on :macos
  depends_on "tmux"

  def install
    libexec.install Dir["*"]
    libexec.install ".env.example"
    (bin/"conduct").write <<~SH
      #!/bin/bash
      exec "#{libexec}/conduct.sh" "$@"
    SH
  end

  def caveats
    <<~EOS
      Two tools install themselves outside Homebrew and are still needed:

        curl -LsSf https://astral.sh/uv/install.sh | sh
        curl -fsSL https://claude.ai/install.sh | bash

      (an arm64 uv at ~/.local/bin/uv is preferred over a Homebrew uv,
      which can be an Intel build with no wheels for this app)

      Then run `claude` once to log in, and start it with:  conduct
      The first run asks for your OpenAI API key and walks through the
      macOS permissions.
    EOS
  end

  test do
    assert_predicate libexec/"conduct.sh", :executable?
  end
end
