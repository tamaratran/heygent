# heygent

**[Download for Mac](https://github.com/tamaratran/heygent/releases/latest/download/heygent.zip)** (Apple silicon)

1. Double-click `heygent.zip` to unzip it.
2. Drag `heygent.app` into your **Applications** folder.
3. Open it. macOS shows its standard "downloaded from the Internet" prompt
   saying Apple checked it - click **Open**.

The first launch installs what the app needs (a minute or two), then asks
for your **OpenAI API key** and, if Claude Code is not signed in on your
Mac, to run `claude auth login`. It also asks for the macOS permissions it
uses (Microphone, Input Monitoring for the Fn key; Accessibility and Screen
Recording for computer-use tasks) and says which switch to turn on. After
that, opening the app just starts it: hold **Fn**, talk, let go.

If Fn does nothing, Input Monitoring is the usual reason: in System
Settings > Privacy & Security > Input Monitoring the switch next to
**heygent** must be on (click + and pick heygent from Applications if it
is not listed), and heygent must be quit and opened again after turning it
on. Fn here is the Mac's own key (the globe key on newer keyboards); on most
non-Apple keyboards the Fn key is handled inside the keyboard and never
reaches macOS.

You need an [OpenAI API key](https://platform.openai.com/api-keys) on an
account with credit (it uses GPT Live) and a Claude account (Pro/Max, or an
Anthropic API key with credit) for the assistant behind it.
