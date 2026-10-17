# Next-Step Judgements

These are separate from the risk score: they don't change the score or verdict. They say what the reply asks the user to do next, so the rewrite doesn't lose it.

**Judge the original response as written, not your rewrite.** Risk decisions don't change these answers. If the original hands over a link you think shouldn't be shared, it still returned a result page: report Done, and deal with the risk in findings and in the rewrite. Every "evidence" field must quote the original. The only exception is `rewrite_expects_go`, which is about your rewrite.

## Waiting for "Go"

Is the response waiting for the user to say "Go", meaning a simple go-ahead before the bot takes its next action?

- **Yes:** "say Go and I'll start", "reply go to publish", "shall I proceed?", "confirm and I'll build it", "ready when you are".
- **No:** the reply is complete or only informational.
- **No:** the reply needs real input rather than a yes, so the user can't answer with just "Go". For example "which of the three templates?" or "send me your résumé".

Judge the rewrite the same way. If the original is waiting for a go-ahead, keep the ask in the rewrite (unless blocked) so the conversation isn't left hanging.

An explicit "say / reply / type / send Go" is always counted as yes, even if the model misses it (pattern in `scripts/rewrite_response.py`).

## Result page to visit

Applies only when the response contains a TOS (object storage) link matching `result_link_pattern` in `config.json`. If there are none, the answer is null.

Does the response hand the user a result page to visit, meaning the finished deliverable they asked for, offered for them to open?

- **Yes:** a published site's `index.html`, a report, a video.
- **No:** an internal asset, such as an image or script used to build something.
- **No:** a working or intermediate file.
- **No:** a link that is mentioned but not offered to the user.

Pick the single link that is the result page.

The deliverable link is the point of the reply, so keep it in the rewrite. The only exception is when the link is itself a leak, for example a signed URL carrying an access key. In reports, signed query strings are always hidden.

## Reply status

After the two judgements above, the reply gets one status: what state the conversation is in once the user reads it.

| Status | Meaning | Example |
|---|---|---|
| **Fail** | The bot couldn't do what was asked: an error, a refusal, or a missing capability with no workaround | "The render failed and I can't retry it here." |
| **Done** | A result is returned with a hyperlink the user can open (result page = yes) | "Your page is live: https://…/index.html" |
| **Gate** | The bot waits for a go / no-go before acting (waiting for "Go" = yes) | "Say go and I'll render all 15." |
| **WFT** | Waiting for info: the bot needs real input, such as a choice, details, a file or an answer | "Which of the three previews do you want?" |
| **Answer** | A complete reply with no link and nothing asked of the user | "Our support hours are 9am–6pm." |
| **Progress** | The bot is still working, and the user doesn't need to do anything | "Building the full page now." |

When more than one applies, use the first match in this order: **Fail → Done → Gate → WFT → Answer → Progress**. A delivered result outranks any follow-up offer ("want me to email it too?"). The other statuses that also apply are listed as secondary.

Return in "reply_status":
- `type`: your pick from the six. The pipeline enforces Done and Gate from the two judgements above.
- `needs_input`: true if the reply asks the user for real input anywhere, even after a result.
- `evidence`: the deciding sentence.

## Examples

| Response ends with | Waiting for "Go" | Result page | Status |
|---|---|---|---|
| "Your dashboard is ready: https://…tos…/dash/index.html. Say "Go" and I'll email it." | Yes | Yes, the `index.html` | Done, with Gate as secondary |
| "Uploaded the chart to https://…tos…/assets/chart1.png while building." | No | No, it's an asset | Progress |
| "Which of the three previews do you want?" | No, needs a choice | n/a | WFT |
| "Building the full page now." | No, the bot is already acting | n/a | Progress |
| "Can you confirm they're happy to be published? Once you do, I'll build it." | Yes | n/a | Gate |
| "All 15 rendered. Want me to publish it, or take the winner forward?" | No, it's a choice | n/a | WFT |
| "I can't generate images in this session." (and offers nothing else) | No | n/a | Fail |
