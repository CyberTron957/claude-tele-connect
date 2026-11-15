# Phone calls with claude-tele-connect (agent guide)

`claude-tele-connect` dials from the user's own Android phone over adb. An AI voice agent talks on the
call; you give it the goal and the facts it needs, wait for the call to finish, then read the result.
The voice agent (Gemini Live or OpenAI Realtime, whichever the user set up) always says it is an AI
assistant calling on the user's behalf.

## 1. Make sure the tool works

Run `claude-tele-connect --version`. If the command is missing, install it and set it up: see the README's install prompt, or
run `claude-tele-connect guide setup` once installed.

On first use, or after any error about the phone or the AI voice, run `claude-tele-connect status --json`.
If `ready` is false, follow `claude-tele-connect guide setup` to fix what `next` names (most fixes need the user to act on
the phone, or to paste a key into the form `claude-tele-connect key` opens). Never ask the user to paste an
API key into the chat.

## 2. Make sure the user asked for this call (ask only when needed)

Calls go out from the user's personal number to real people, so only dial a call the user asked for. Don't
ask for confirmation just to be safe: when the request is specific, it is the approval.

1. Work out who to call. For a contact name, run `claude-tele-connect lookup "<name>" --json` and use the number it
   returns.
2. **Just place the call** (say in one line who you're calling and the goal as you go) when the user's request
   names who to call (a number they gave, or a contact that matches exactly one number) and what the call
   should achieve, and you have the facts the call needs.
3. **Ask once, in one short message**, only when something is unclear: several contact matches or none, a goal
   or fact the call needs is missing, the number came from your own web search rather than from the user, or
   the call would commit the user to money, a purchase or a booking they haven't described. Put every open
   question in that one message, and don't ask again after the answer.
4. If the user has said not to ask before calls, never ask, beyond the exceptions in 3 about money and
   unclear numbers.

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

- `--yes` skips the terminal prompt. Use it when step 2 is satisfied.
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
| "... isn't configured" / "your name isn't set" / a missing key | Follow `claude-tele-connect guide setup`: `claude-tele-connect status --json` says what to do. |
| "phone is already in a call" | Wait until the user's current call ends. |
| `ended_by` is "phone audio never became ready" | Run `claude-tele-connect check --json`; the phone may need Bluetooth audio mode. |
