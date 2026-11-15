---
name: call
description: Place a real phone call from the user's own Android phone and number, with an AI voice that talks to the other person to reach a goal, then report what happened. Use when the user asks you to call someone or a business (book or change an appointment, ask a question, check opening hours, pass on a message, navigate a phone menu). Requires the claude-tele-connect command-line tool and an Android phone connected over adb.
---

# Phone calls with claude-tele-connect

Run `claude-tele-connect --version`; if it is missing, use the **setup** skill first. Then run
`claude-tele-connect guide call` and follow what it prints.

Only dial calls the user asked for. A specific request (who to call, what for) is the approval; ask once only
if something is unclear. Never ask for an API key in the chat.
