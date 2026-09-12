# Voice Agent

Hold **Fn**, talk, and run coding agents on your Mac by voice.

## Download

**[Download Voice Agent for Mac](https://github.com/tamaratran/heygent/releases/latest/download/Voice-Agent.zip)**
(Apple silicon; all versions on the [releases page](https://github.com/tamaratran/heygent/releases/latest))

1. Double-click `Voice-Agent.zip` to unzip it.
2. Drag `Voice Agent.app` into your **Applications** folder.
3. Open it. macOS shows its standard "downloaded from the Internet" prompt
   saying Apple checked it for malware - click **Open**.

## First run

The first launch opens a Terminal window that installs what the app needs
(uv, tmux, Claude Code - via Homebrew if you have it), asks for your
**OpenAI API key**, and walks through the macOS permissions (microphone,
accessibility, screen recording). Sign in to Claude Code with your Claude
account if it asks. After that, opening the app just starts it.

## Use it

Hold **Fn** and say what you want done. Let go, and the agents get to work.

## Command line instead

```bash
curl -fsSL https://raw.githubusercontent.com/tamaratran/heygent/main/install.sh | bash
conduct
```

## Something wrong?

Logs live in `~/.voice-conductor/logs/`. Open an
[issue](https://github.com/tamaratran/heygent/issues) with the last lines
of the newest `conductor-*.jsonl` there.
