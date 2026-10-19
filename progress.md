# AT Project — Progress

Live status. Update after every significant edit. This is how future-you (and agents) know where things stand.

---

**Last updated:** 2026-XX-XX HH:MM
**Current phase:** Phase 1 — Foundation
**Current module:** at-ipc
**Next module:** at-config

---

## Phase Status

### Phase 0 — Specs (DONE)
- [x] AT Vision Document v0.4
- [x] Voice Loop MVP v0.4
- [x] IPC Protocol v0.1
- [x] Config Loader v0.1
- [x] Audio Source v0.1
- [x] Wake Word v0.2
- [x] VAD v0.2
- [x] STT v0.2
- [x] LLM Runtime v0.2
- [x] TTS v0.2
- [x] Loop Controller v0.2
- [x] Repo structure
- [x] Decision log
- [x] Architecture overview

### Phase 1 — Foundation (IN PROGRESS)
- [ ] at-ipc implemented
- [ ] at-ipc tested
- [ ] at-config implemented
- [ ] at-config tested

### Phase 2 — Audio Input
- [ ] audio-source implemented
- [ ] audio-source tested with WAV capture

### Phase 3 — Wake
- [ ] Training data recorded
- [ ] Model trained (cycle 1)
- [ ] Model trained (cycle 2+)
- [ ] wake-word service implemented
- [ ] wake-word tested in-room

### Phase 4 — Speech In
- [ ] vad implemented and tested
- [ ] stt implemented and tested

### Phase 5 — Speech Out
- [ ] tts implemented and tested

### Phase 6 — Brain
- [ ] personality.md drafted
- [ ] llm implemented
- [ ] Reference prompts written (20)
- [ ] llm benchmarked against reference prompts

### Phase 7 — Conductor
- [ ] loop implemented
- [ ] Full loop wired end-to-end

### Phase 8 — Integration & Tuning
- [ ] Full acceptance test suite passing
- [ ] Latency within targets
- [ ] 24-hour stability run
- [ ] Personality polish

---

## Current Blockers

None.

---

## Recent Sessions

### 2026-XX-XX — Specs finalized
- All eleven specs frozen
- Repo structure created
- Decision log seeded

### 2026-XX-XX — at-ipc kickoff (pending)
- Waiting for agent plan

---

## Notes for Next Session

- Paste `at-ipc` prompt into claude-opus4.6(thinking)
- Review plan before approving
- Bring gemini3.8-flash in for cross-family review after implementation