# RIDGELINE judging review

Updated 2026-11-08, America/New_York. Owner Codex.

## Scope and branch

Worktree C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline

Branch `codex/ridgeline-judging-review`, based on `ridgeline-live` at `9cb295b2442338795826bdfcd88a7bd43122bc43`. The main checkout at `C:/Users/seika/dev/Team-8` still has the separate PR #6 merge in progress and was not changed. The active phone worktree and port 8768 were not changed. This review is not yet on main or on the physical phone.

## Completed

- Compared Manus local remote-tracking commit c008191 against RIDGELINE 9cb295b. Adopted the evidence-recording approach. Excluded the duplicated Manus route engine after reproducing water-crossing smoothed segments and diagonal corner crossing on synthetic grids. Remote PR status could not be independently refreshed through the connected GitHub tool.
- Replaced the root README skeleton with actual setup, recorded observations, boundaries and deployment steps. Preserved Credits. Team members known only by handle still need name and role confirmation.
- Added four-criterion evidence mapping and an unfilled five-trial protocol. Historical wall observations are not new measurements of this revision.
- Corrected GPS test claims, removed a false automatic jamming/switching claim, and described the alternate route origin as an unverified candidate. Removed its connecting line. No solver or route algorithm replacement. Updated service-worker version.
- Added a standard-library trial recorder. It reads state only, saves failures locally, distinguishes operator declarations, and flags duplicate Lock observations even after GPS, health or height settings change.

## Validation

- Node tests 28 passed, including solver, Hide, skyline and all three areas of water routing.
- Python tests 20 passed, including CoT and 10 recorder fixtures.
- Phone and board inline JavaScript syntax checks passed; git diff --check passed.
- Browser on isolated port 8809, keyboard-assisted three Locks to fix and route; far GPS test point displayed GPS DISAGREES; injured mode displayed an unverified candidate start without a connector. Screenshots are in .local/gps-review.png and .local/candidate-start-review.png.
- Recorder CLI captured the keyboard run with synthetic-reference error 67 m and r95 304 m. The second capture with a new trial ID was correctly duplicate. This is not a physical-phone trial. .local/review-trials-v2.jsonl uses the final fingerprint scheme; .local/ridgeline-trials.jsonl is an earlier developer smoke record and must not enter Gate B.
- Mobile viewport override returned an unreliable scaled capture. Do not claim a 390 px mobile or 1600 x 900 terrain-viewer visual pass from this session. Normal browser layout was inspected.

## Next owner and remaining work

RIDGELINE integration owner should review this branch diff and cherry-pick its review commit onto the current ridgeline-live after checking concurrent changes to phone/index.html, board/index.html and README. Re-run the affected tests and reload the physical phone while connected so the new service worker is installed. Do not abort the main checkout merge without Seika changing the earlier keep-as-is decision.

Seika performs the five consecutive real-phone trials and an unfamiliar-user trial. Record every failure. Airplane mode and actual ATAK rendering still need a physical check. Current results do not establish real-mountain accuracy or spoofing detection. Complete main integration before the stated freeze/submission schedule and verify the final submission tag separately; no submission tag was created here.

## Changed files

- [README.md](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/README.md)
- [docs/JUDGING_EVIDENCE.md](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/docs/JUDGING_EVIDENCE.md)
- [ridgeline/README.md](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/ridgeline/README.md)
- [ridgeline/phone/index.html](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/ridgeline/phone/index.html)
- [ridgeline/phone/sw.js](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/ridgeline/phone/sw.js)
- [ridgeline/board/index.html](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/ridgeline/board/index.html)
- [ridgeline/cot.py](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/ridgeline/cot.py)
- [ridgeline/test/test_cot.py](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/ridgeline/test/test_cot.py)
- [ridgeline/scripts/capture_trial.py](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/ridgeline/scripts/capture_trial.py)
- [ridgeline/test/test_capture_trial.py](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/ridgeline/test/test_capture_trial.py)
- [STATUS.md](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/STATUS.md)

## Local artifacts and references

- [Trial log](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/.local/review-trials-v2.jsonl)
- [Earlier smoke log, excluded from Gate B](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/.local/ridgeline-trials.jsonl)
- [GPS screenshot](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/.local/gps-review.png)
- [Candidate screenshot](C:/Users/seika/.codex/worktrees/ridgeline-judging-review/Team-8-ridgeline/.local/candidate-start-review.png)
- [Decision note](C:/Users/seika/Obsidian/2_Areas/BHCC/マネーハンティング/2026-11-08_CodeMetalForge_審査前レビューとManus採用判断.md)
- [Recorder manual](C:/Users/seika/Obsidian/2_Areas/AIツール取説/RIDGELINEの実演計測（Team-8）.md)
- [Manual index](C:/Users/seika/Obsidian/2_Areas/AIツール取説/取説一覧.md)
- [Shared status](C:/Users/seika/Obsidian/STATUS.md)

Review server was started on loopback port 8809 with CoT disabled, separate from the physical-phone server. If it is no longer running, use `python -I ridgeline/server.py --port 8809 --cot-port 0` from this worktree.
