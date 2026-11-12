---
name: call
description: Place a real phone call from the user's own Android phone and number, with an AI voice (Gemini Live) that talks to the other person to reach a goal, then report what happened. Use when the user asks you to call someone or a business (book or change an appointment, ask a question, check opening hours, pass on a message, navigate a phone menu). Requires the claude-tele-connect command-line tool and an Android phone connected over adb.
---

# Phone calls with claude-tele-connect

`claude-tele-connect` dials from the user's own Android phone over adb. A Gemini Live voice agent talks on the
call; you give it the goal and the facts it needs, wait for the call to finish, then read the result.
The voice agent always says it is an AI assistant calling on the user's behalf.

## 1. Make sure the tool works

Run `claude-tele-connect --version`. If the command is missing, tell the user to install it
(`pipx install git+https://github.com/<owner>/claude-tele-connect`, see the project README) and stop.

On first use, or after any error about the phone or Gemini, run `claude-tele-connect check`. It reports whether the
phone is connected, whether Gemini is configured, and which audio mode the phone needs. If it says
"Fix the items above", relay those items to the user in plain words; most need the user to act on the
phone (turn on wireless debugging, pair, plug in USB) or to run `claude-tele-connect setup` in their own terminal,
which asks for a Gemini API key. Never ask the user to paste an API key into the chat.

## 2. Confirm the exact call with the user (required, every call)

Calls go out from the user's personal number to real people, so nothing is dialed until the user has
approved this specific call in the conversation.

1. Work out who to call. For a contact name, run `claude-tele-connect lookup "<name>" --json` and use the number it
   returns. If there are several matches or none, ask the user which number to use.
2. Show the user, in one short message: who (name and number), the goal, the facts the AI will use,
   and the language if it is not English. Ask "Shall I place this call?"
3. Only a clear yes to that message counts. A yes to an earlier, different call does not.

Refuse, and say why, if the request is: an emergency number (tell the user to call emergency services
themselves; the tool also blocks these), cold calling or marketing, calling many numbers in a row,
harassing or pranking someone, pretending to be a human or a specific real person, or getting personal
information about someone under false pretences.

## 3. Write a good goal and context

The voice agent knows only what you pass it. It cannot see this conversation or ask you questions during
the call.

- `--goal`: one or two sentences saying what a successful call achieves and when to stop, e.g.
  "Book a haircut for Saturday morning, any time 9-12. If Saturday is full, ask for Sunday and don't
  book anything later than that."
- `--context`: every fact the other side may ask for that the user has agreed to share: the name the
  booking is under, dates, order numbers, preferences, limits ("don't agree to pay anything").
  Leave out anything the user didn't want shared.
- If the other person will speak another language, say so in the goal ("Speak Hindi").
- Phone menus: the agent can press keys itself; mention the option to pick if you know it.

## 4. Place the call

```
claude-tele-connect call "<number or contact>" --goal "<goal>" --context "<facts>" --yes --json
```

- `--yes` skips the terminal prompt. Use it only after the user approved the call in step 2.
- Calls last up to `--max-minutes` (default 5) plus ringing, so give the command a long timeout
  (10 minutes) or run it in the background and wait for it to finish. Don't start a second call
  while one is running.
- Add `--max-minutes 2` for quick questions. Add `--record` only if the user asked for a recording.

## 5. Report back

stdout is one JSON object. Useful fields:

- `outcome`: the voice agent's own one-line summary
- `ended_by`: `agent`, `other side hung up`, `not answered`, `time limit`, or an error
- `transcript`: list of `{t, speaker, text}`; speaker is `AI` or `Them`
- `transcript_file`: where the transcript was saved

Tell the user what actually happened, based on the transcript, not just `outcome`: what was agreed,
times, prices, reference numbers, and anything left open. If the other person's answer was unclear, or
the call failed or wasn't answered, say so plainly and offer to try again. Don't claim something was
booked or agreed unless the transcript shows the other person clearly confirmed it.

## Troubleshooting

| Message | What to do |
|---|---|
| "No authorized phone found over adb" | Phone not reachable. Ask the user to check Wireless debugging is on (or plug in USB), then retry. Wireless debugging turns itself off when the phone changes Wi-Fi. |
| "Gemini isn't configured" / "your name isn't set" | The user runs `claude-tele-connect setup` in their terminal. |
| "phone is already in a call" | Wait until the user's current call ends. |
| `ended_by` is "phone audio never became ready" | Run `claude-tele-connect check`; the phone may need Bluetooth audio mode. |
