# The Argument audio reports — Task 7 pilot (2026-10-18)

Phase 1 code at `b77b8cc` plus plan commits. Command, per episode:

```bash
uv run python -m pipeline podcast-report dry-run --source argument --guid <GUID>
```

Run as transient user units (`systemd-run --user`, `MemoryMax=4G`) under
`/run/current-system/sw/bin/time -v`. Both runs were concurrent. Logs:
`pilot-evidence/oct15.log`, `oct08.log` (see below).

## Outputs

| | Oct 15 "Canceling (and un-canceling) historic heroes" | Oct 8 "Trump, Xi, and the race for superintelligence" |
|---|---|---|
| GUID | `audioboom-01a13c3d-…` | `audioboom-01a1185a-…` |
| Work dir | `/persist/my-podcasts/podcast-work/dry-run/argument/0aea318dc8309490/` | `/persist/my-podcasts/podcast-work/dry-run/argument/40379bf3303132d1/` |
| Script | `…/0aea318dc8309490/script.txt` (1619 words) | `…/40379bf3303132d1/script.txt` (1762 words) |
| Participants | hosts only | hosts + Jordan Schneider (ChinaTalk) |

## Measurements

| | Oct 15 | Oct 8 |
|---|---|---|
| RSS duration | 4807 s | 5152 s |
| Decoded duration | 4947.6 s (+141 s) | 5313.1 s (+161 s) |
| Duration tolerance | max(120, 0.08×rss) = 385 s, passed | 412 s, passed |
| ffmpeg warnings | none | none |
| Pieces | 9 (last is a 102 s tail, 257 words) | 9 |
| ASR attempts | 1 per piece, all `STOP`, no retries | same |
| Words/piece (≥120 s pieces) | 1894–2135 (≈190–210 wpm) | 1702–2030 |
| Gemini tokens in/out | 137,231 / 22,146 | 156,144 / 23,926 |
| Writer input | 90,875 chars, 16,059 words | 97,013 chars, 17,144 words |
| Wall: download / transcribe / write | 4.1 s / 197.3 s / 50.1 s | 5.6 s / 210.7 s / 45.4 s |
| Wall total | 4:14 | 4:25 |
| Peak RSS | 618 MB | 654 MB |

The decoded audio runs 141–161 s longer than the RSS duration, because of
dynamic ad insertion (the CDN URL says `media_type=dynamic`,
`general_run_ads`). That is inside the tolerance, but it uses 37–42% of it.
Phase 2 child deadline: transcription ran about 22 s per 10-minute piece, done
in order. The 97-minute Sep 10 episode should take about 4 minutes, so a
deadline of 2× the measured time plus margin is about 15 minutes.

## Speaker attribution

**Names held across pieces in both episodes.** Each episode used one stable
name set from piece 1 through piece 9: `Matt` / `Jerusalem` in Oct 15, and the
full names, including `Jordan Schneider`, in Oct 8. No label drifted,
split or swapped between pieces. `Unknown speaker *` appears only in the first
and last pieces, about 1–5 turns each, around the intro/promo and outro reads.

**I could not listen to the audio.** No human ear check has been done yet.
Instead, I sampled 50 s clips at the intro and on both sides of every piece
boundary (17 clips per episode) and compared the transcript's labels with
two independent measures:

1. **A voice-only relabel.** A fresh Gemini call heard each clip with no
   names and no context. It labelled turns `FEMALE`, `MALE-1` or `MALE-2`. Its
   words were then aligned into the transcript.
2. **Pitch.** An autocorrelation F0 estimate per second, where median under
   160 Hz counts as male and over 175 Hz as female. Demsas is the only female
   voice, so this checks her against the two men. It cannot separate Yglesias
   from Schneider. Calibrated on clips where both other measures agreed: all-female
   clips read 0% male, and all-male clips read 80–91% male. It is noisy but
   separable.

Results (all evidence copied to `/persist/my-podcasts/podcast-work/dry-run/argument/pilot-evidence/`: `attr-*.txt`, the two scripts `attrcheck.py`/`pitch.py`, run logs, and
`clips/` with each relabel `.txt`):

- In 13/17 Oct 15 clips and 14/17 Oct 8 clips, the transcript's gender split
  matched the relabel, and where measured, the pitch.
- Where they disagreed, pitch usually sided with the transcript. The
  voice-only relabel tended to merge a short second-voice turn into the main
  speaker. Examples: Oct 15 @19:25 had transcript male 41%, relabel 0%, pitch 25%.
  Oct 15 @70:00 had 48% / 0% / 41%. Oct 15 @80:45 had 28% / 81% / 26%.
- Transcript errors found:
  - **Small.** Oct 15 @09:07: 9 of 183 words (5%) were labelled Matt in an
    all-female stretch, which both measures confirm. Oct 15 @79:55: the
    transcript says male 55% where pitch says 31%, so it overstates Matt.
  - **One open conflict.** Oct 8 @69:43 is the outro stretch, crowded
    with three voices. The transcript says Demsas 33%. Pitch says about 70%
    female. The relabel collapsed everything to male voices. Unresolved. It is
    the best clip for an ear check.
- Male/male (Yglesias vs Schneider) attribution is **not independently
  checked**. The only evidence is internal consistency: Schneider's turns carry
  ChinaTalk/China-specific content, and the name set never swapped.

**Read for the decision:** no drift across pieces, and errors are at the level
of a few sentences, not systematic misattribution. The report prompt already
hedges unknown speakers. The scripts attribute by name throughout, which is
fine if the owner's ear check agrees.

## Scripts (qualitative)

Both scripts read as good spoken briefings. They name the participants
correctly, follow the conversation in order with concrete details (Harrison
Playground / Cardozo School / Pulaski Skyway; the Pangram, Grok and
congestion-pricing threads), cover the Peer Review segment, and contain no ad,
sponsor, Patreon or subscribe mentions (grep: 0). They run 1619 and 1762 words,
above the prompt's 800–1500 target: about 10–11 minutes of audio rather than
5–10. Tighten the prompt if the owner wants them shorter.

## Follow-ups noted (not done)

- The dry-run CLI's INFO logging shows httpx request lines, including the full
  signed CDN URLs. That is noisy. Set `httpx` to WARNING in the command.
- Dynamic ads use about 40% of the duration tolerance. Track the
  `decoded - rss` delta per episode in Phase 2 and alert if it trends toward the
  limit, rather than widening the limit blind.
- Phase 2 notes from the reviews are recorded in the plan's "Handoff state".

## Decision needed (owner)

1. Listen to a few spots in either script's episode and confirm attribution.
   The Oct 8 @69:43 clip is the best one. Clips can be shared on request.
2. Choose one:
   - **Proceed to Phase 2 as designed** (names as produced).
   - Tune the context/prompt and re-pilot.
   - Keep the reports but have them avoid naming who said what.
3. Optional: script length. Keep about 10 minutes, or tighten to the 5–10
   target.
