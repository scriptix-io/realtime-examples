# Parakeet vs Kaldi — edge-case test report

Date: 2026-05-26. Harness: `python-websockets/run_all.py` (this folder).

## Backends tested

| backend | endpoint | language under test | image / version |
|---|---|---|---|
| Parakeet CPU (staging) | `wss://realtime.scriptix.dev/v2/realtime` | `en` | `scriptix.azurecr.io/api/realtime-cpu:0.1.6` |
| Kaldi prod | `wss://api.scriptix.io/realtime` | `nl-pol` | legacy Tornado proxy → `nl-pol-asr` |

Kaldi cluster note: of 14 `{lang}-asr` deployments, only `nl-pol-asr` is
scaled up. Every other language is `0/0`. End-to-end Kaldi testing is
limited to `nl-pol` until those scale up.

Audio fixtures:
- `en.wav` — 7.4 s clip from the streaming Parakeet bundle test set.
- `nl.wav` — 12 s captured from BNR Radio livestream (16 kHz mono).

## Connection / handshake

| | parakeet | kaldi |
|---|---:|---:|
| TLS + auth | ✅ | ✅ |
| `{"action":"start"}` → `{"state":"listening","session_id":...}` | ✅ 110 ms | ✅ 101 ms |
| TCP connect | 530 ms | 297 ms |

Both backends accept `x-zoom-s2t-key: <token>` and respond with the
same handshake message shape. Drop-in compatible.

## Wire format — now unified

After this commit (`6decf27`) Parakeet emits the **Kaldi shape**.
Existing customer clients keep working:

```jsonc
// partials
{"partial": "well, I don't"}

// final
{"result": [["well,", 0, 320, 0.99], ["I", 320, 410, 1.00], ...],
 "speaker": "Speaker 1",
 "text": "well, I don't wish to see it anymore"}

// control
{"state": "listening", "session_id": "7079f6cc..."}
{"state": "stopped"}
```

Pre-fix Parakeet emitted `{"text": ..., "is_final": ..., "offset_ms":
..., "stability": ...}` (Whisper v2 format). Old format dropped — only
the Kaldi shape is on the wire now.

## Edge-case matrix

Each test ran on both backends.

| test | what it does | parakeet 0.1.6 | kaldi prod | gap |
|---|---|---|---|---|
| **T1 normal** | full audio + `{"action":"stop"}` | 6 partials, **no final**, first partial 10.8 s | 56 partials, 1 final, first partial 1.4 s, first final 13.0 s | **first-partial latency 10s** vs 1.4s; **no final emitted on stop** in some runs |
| **T2 silence** | 5 s of zeros + stop | 0 messages, `{"state":"stopped"}` after 17 s | 0 messages, `{"state":"stopped"}` in 1.3 s | Parakeet stop is **slow** for silence; Kaldi exits cleanly |
| **T3 short 500 ms** | 500 ms audio + stop | 0 messages, hits 3-strike rate-limit on repeat | 0 messages, `{"state":"stopped"}` in 1.8 s | Parakeet has no graceful "too-short" handling and the **rate-limiter is too aggressive** under test churn |
| **T4 idle then speech** | wait 15 s, then send audio | hangs 29 s — no partials | 16 partials + 1 final after the idle, behaves normally | **Parakeet survives WS idle but never re-engages decode**; needs investigation |
| **T5 mid-drop** | close WS halfway, no `stop` | 0 messages received before drop | 26 partials before drop | Parakeet **too slow to deliver anything within the 3-second window** |
| **T6 reconnect leg B** | fresh WS for second half of audio | partials returned, no final | 26 partials + 1 final | Same finals-on-stop gap as T1 |

## Where Parakeet falls short of Kaldi

P0 — must close before production:

1. **First-partial latency**: 10 s observed vs Kaldi's 1.4 s. Local
   container smoke (engine direct, bypassing `AudioProcessor`) showed
   first partial at offset_ms 1.8 s, so the lag is being introduced
   in the WS / processing layer — not the engine. Likely candidates:
   producer-side buffering coalescing 200 ms WS chunks into multi-
   second batches before the processor wakes up, or sherpa-onnx
   needing more warmup audio than the 100 ms `MIN_CHUNK_BYTES`
   threshold allows. Needs `--debug` log run inside the pod.

2. **No `{"state":"stopped"}` for non-speech sessions**: T2 / T3 hang
   for tens of seconds before the stop frame lands. `audio_processor.
   stop()` now drains then finalizes, but the path for "buffer empty
   + no transcript" doesn't short-circuit to `notify_state_stopped`
   fast enough.

3. **Rate-limit too aggressive in test loops**: 3 rejects in 10 min
   blocks the token until the window expires. Fine for abuse but a
   regular reconnect loop during testing hits it. Either move the
   limit to per-IP or whitelist staging tokens.

P1 — should fix before declaring "as reliable as Kaldi":

4. **Session resume on reconnect**: T6 already shows fresh state on
   the new socket. Tracked in HANDOFF.md.

5. **Word confidence is real on Kaldi**, hardcoded `1.0` on Parakeet
   for now. sherpa-onnx exposes `result.ys_probs` — wire it through
   to `SpeechToTextResultWord.confidence`.

6. **Long-running idle**: T4 indicates the audio processor may detach
   from the engine if the WS idles. Verify after the latency root
   cause is found.

## Kaldi observed strengths

- First partial **1.2–1.7 s** consistently across runs.
- Continuous partials throughout the utterance (~1 every 200 ms).
- Final with per-word `[word, start_ms, end_ms, confidence]` array,
  consistently within ~12 s of audio end.
- Empty / silent sessions get `{"state":"stopped"}` in **< 2 s**.
- Mid-drop doesn't fail subsequent reconnects — no rate-limit.

## Reproducing this report

```bash
# tokens (refresh from Scriptix app)
export REALTIME_TOKEN=...
export KALDI_TOKEN=...

# fetch fixtures (one time)
mkdir -p /tmp/asr-test
curl -sSL -o /tmp/asr-test/en.wav https://huggingface.co/csukuangfj2/sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-streaming-560ms/resolve/main/test_wavs/0.wav
ffmpeg -loglevel error -i https://stream.bnr.nl/bnr_mp3_128_20 -t 12 -ac 1 -ar 16000 -acodec pcm_s16le /tmp/asr-test/nl.wav

# run matrix (tokens are hardcoded in this version; will refactor to env)
python python-websockets/run_all.py
```

Raw runs saved at `/tmp/asr-test/report4.txt` from the 2026-05-26 session.
