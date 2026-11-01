# Browser video health

`connect()` resolves when the transport and declared channels are ready. It does
not promise a decoded or displayed video frame. The server's initial media
deadline observes incoming RTP and cannot measure a receiving browser's video.

Generated clients export the optional `observeVideoHealth(session, options)`
helper. Both deadlines are disabled by default; enable thresholds appropriate to
the stream and UI. The following numbers are example application policy:

```ts
import { observeVideoHealth } from "./rtc/index.js";

// Attach remote video to this element and start playback first.
const observer = observeVideoHealth(session, {
  video,
  firstFrameTimeoutMs: 10_000,
  stallTimeoutMs: 5_000,
  onChange: ({ state, reason, measurement }) => {
    console.log(state, reason, measurement);
    // The application decides whether to show a warning or switch transports.
  },
});

// On component disposal; session closure also stops observation automatically.
observer.stop();
```

The observer starts in `waiting`. A first observed frame changes it to `healthy`;
an enabled deadline changes it to `stalled`, with `first_frame_timeout` or
`stall_timeout`. Progress after a stall recovers automatically. `stopped` is
terminal. The helper never closes the session, stops tracks, changes playback or
chooses a fallback. `signal` can stop observation independently of the session.

With a supported video element, `measurement="presented"` observes
`requestVideoFrameCallback`: a frame was submitted to the compositor. This does
not prove the user saw it, or that the video is visible and unobscured. Without
that API, the observer polls inbound video `framesDecoded`, reporting
`measurement="decoded"`. It defaults to a 500 ms polling interval and never
overlaps stats requests. A blocked stats call cannot postpone the separate
first-frame deadline. Missing counters are unknown and do not count as progress.

For stats from one track, supply `trackId` equal to its `MediaStreamTrack.id`.
Reports without the matching `trackIdentifier` cannot establish progress for that
track. With a video element, that element selects the displayed stream instead.
Use a separate observer for each preview. Counter resets and replacement SSRCs
start new observation epochs. Callback and stats failures can be inspected with
`onError`; they never become WebRTC protocol failures.

Thresholds must account for intentionally sparse screen updates, paused playback
and background-tab throttling. A deadline means no progress was observed in its
window, not proof of a failed network. Suspend or recreate observers when the
application intentionally stops presenting media. Cleanup cancels timers and
frame callbacks and ignores late stats results.
