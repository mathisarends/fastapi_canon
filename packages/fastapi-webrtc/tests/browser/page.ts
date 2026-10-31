// Browser-side scenarios for the Playwright tests; each returns plain JSON.
import { PracticeRtcClient, isRtcError, observeVideoHealth, type VideoHealthEvent } from "../../examples/practice/client/index.js";

const client = new PracticeRtcClient({ baseUrl: location.origin });

async function until(condition: () => boolean, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error("Timed out");
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

async function practice(): Promise<Record<string, unknown>> {
  const microphone = await navigator.mediaDevices.getUserMedia({ audio: true });
  const remoteKinds: string[] = [];
  const feedback: { text: string; audio_frames: number }[] = [];
  const session = await client.practice.connect({
    lessonId: "greetings",
    token: "demo",
    tracks: microphone,
    onTrack: ({ track }) => void remoteKinds.push(track.kind),
    onMessage: { messages: (message) => void feedback.push(message) },
  });
  await until(() => remoteKinds.length > 0);
  // Give the server time to receive audio frames before asking for feedback.
  await new Promise((resolve) => setTimeout(resolve, 1000));
  await session.channels.messages.send({ text: "hello tutor" });
  await until(() => feedback.length > 0);
  let inboundAudio = 0;
  (await session.stats()).forEach((report: { type: string; kind?: string; packetsReceived?: number }) => {
    if (report.type === "inbound-rtp" && report.kind === "audio") inboundAudio += report.packetsReceived ?? 0;
  });
  await session.close();
  const reason = await session.closed;
  return {
    state: session.state,
    remoteKinds,
    feedback,
    inboundAudio,
    reason: reason.code,
    microphoneLive: microphone.getTracks().every((track) => track.readyState === "live"),
  };
}

async function denied(): Promise<Record<string, unknown>> {
  try {
    await client.practice.connect({ lessonId: "greetings", token: "wrong" });
    return { code: null };
  } catch (error) {
    return { code: isRtcError(error) ? error.code : String(error) };
  }
}

async function clock(): Promise<Record<string, unknown>> {
  const ticks: number[] = [];
  let width = 0;
  const session = await client.clock.connect({
    intervalMs: 100,
    onMessage: { ticks: (tick) => void ticks.push(tick.sequence) },
    onTrack: ({ track }) => {
      const video = document.createElement("video");
      video.muted = true;
      video.srcObject = new MediaStream([track]);
      video.onloadedmetadata = () => {
        width = video.videoWidth;
      };
      document.body.append(video);
      void video.play();
    },
  });
  await until(() => ticks.length >= 3 && width > 0);
  const video = document.querySelector("video");
  if (video === null) throw new Error("No remote video element");
  const events: VideoHealthEvent[] = [];
  const presented = observeVideoHealth(session, {
    video, firstFrameTimeoutMs: 5000, stallTimeoutMs: 500,
    onChange: (event) => events.push(event),
  });
  const decoded = observeVideoHealth(session, { firstFrameTimeoutMs: 5000, pollIntervalMs: 50 });
  await until(() => presented.state === "healthy" && decoded.state === "healthy");
  video.pause();
  await until(() => presented.state === "stalled");
  const stalledSessionOpen = session.state === "open";
  await video.play();
  await until(() => events.some((event) => event.reason === "recovered"));
  await session.close();
  await until(() => presented.state === "stopped" && decoded.state === "stopped");
  return {
    ticks: ticks.slice(0, 3), width, state: session.state,
    measurement: events[0]?.measurement, reasons: events.map((event) => event.reason),
    stalledSessionOpen,
  };
}

async function renegotiate(): Promise<Record<string, unknown>> {
  const session = await client.practice.connect({ lessonId: "numbers", token: "demo" });
  const microphone = await navigator.mediaDevices.getUserMedia({ audio: true });
  const [track] = microphone.getAudioTracks();
  if (track === undefined) throw new Error("No microphone track");
  await session.addTrack(track, microphone);
  await until(() => session.remoteTracks.some((remote) => remote.track.kind === "audio"));
  await session.close();
  return { remote: session.remoteTracks.length, live: track.readyState === "live" };
}

Object.assign(window, { scenarios: { practice, denied, clock, renegotiate } });
document.body.dataset.ready = "true";
