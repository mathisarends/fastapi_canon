// In-memory WebSocket and RTCPeerConnection fakes for runtime unit tests.

export type Json = Record<string, unknown>;

export class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  readonly protocols: string | string[] | undefined;
  readyState = 0;
  protocol = "";
  binaryType = "blob";
  sent: Json[] = [];
  closedWith: { code: number | undefined; reason: string | undefined } | null = null;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(url: string, protocols?: string | string[]) {
    this.url = url;
    this.protocols = protocols;
    FakeWebSocket.instances.push(this);
  }

  static last(): FakeWebSocket {
    const socket = FakeWebSocket.instances.at(-1);
    if (socket === undefined) throw new Error("No WebSocket was opened");
    return socket;
  }

  send(data: string): void {
    if (this.readyState !== 1) throw new Error("WebSocket is not open");
    this.sent.push(JSON.parse(data) as Json);
  }

  close(code?: number, reason?: string): void {
    if (this.readyState >= 2) return;
    this.closedWith = { code, reason };
    this.readyState = 3;
  }

  // ------------------------------------------------------------ server side

  open(protocol = "fastapi-webrtc.v1"): void {
    this.protocol = protocol;
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }

  receive(message: Json | string): void {
    const data = typeof message === "string" ? message : JSON.stringify(message);
    this.onmessage?.({ data } as MessageEvent);
  }

  serverClose(code: number, reason = ""): void {
    this.readyState = 3;
    this.onclose?.({ code, reason } as CloseEvent);
  }

  messages(type: string): Json[] {
    return this.sent.filter((message) => message.type === type);
  }
}

export class FakeTrack {
  stopped = false;
  readonly id: string;
  constructor(readonly kind: "audio" | "video") {
    this.id = `${kind}-${Math.random().toString(16).slice(2)}`;
  }
  stop(): void {
    this.stopped = true;
  }
  asTrack(): MediaStreamTrack {
    return this as unknown as MediaStreamTrack;
  }
}

export class FakeDataChannel {
  readyState: RTCDataChannelState = "connecting";
  bufferedAmount = 0;
  bufferedAmountLowThreshold = 0;
  binaryType = "blob";
  sent: unknown[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onbufferedamountlow: (() => void) | null = null;

  constructor(
    readonly label: string,
    readonly init: RTCDataChannelInit = {},
  ) {}

  send(data: unknown): void {
    if (this.readyState !== "open") throw new Error("Channel is not open");
    this.sent.push(data);
  }

  close(): void {
    if (this.readyState === "closed") return;
    this.readyState = "closed";
    this.onclose?.();
  }

  open(): void {
    this.readyState = "open";
    this.onopen?.();
  }

  receive(data: unknown): void {
    this.onmessage?.({ data } as MessageEvent);
  }

  drain(): void {
    this.bufferedAmount = 0;
    this.onbufferedamountlow?.();
  }
}

interface FakeTransceiver {
  mid: string | null;
  direction: RTCRtpTransceiverDirection;
  currentDirection: RTCRtpTransceiverDirection | null;
  receiver: { track: { kind: string } };
  sender: {
    track: MediaStreamTrack | null;
    streams: MediaStream[];
    replaceTrack(track: MediaStreamTrack | null): Promise<void>;
    setStreams(...streams: MediaStream[]): void;
  };
}

let sdpCounter = 0;

export class FakePeerConnection {
  static instances: FakePeerConnection[] = [];

  readonly configuration: RTCConfiguration | undefined;
  signalingState: RTCSignalingState = "stable";
  connectionState: RTCPeerConnectionState = "new";
  onconnectionstatechange: (() => void) | null = null;
  localDescription: RTCSessionDescriptionInit | null = null;
  remoteDescription: RTCSessionDescriptionInit | null = null;
  readonly channels: FakeDataChannel[] = [];
  readonly transceivers: FakeTransceiver[] = [];
  readonly candidates: (RTCIceCandidateInit | null)[] = [];
  readonly log: string[] = [];
  closed = false;
  onicecandidate: ((event: RTCPeerConnectionIceEvent) => void) | null = null;
  ontrack: ((event: RTCTrackEvent) => void) | null = null;
  ondatachannel: ((event: RTCDataChannelEvent) => void) | null = null;

  constructor(configuration?: RTCConfiguration) {
    this.configuration = configuration;
    FakePeerConnection.instances.push(this);
  }

  static last(): FakePeerConnection {
    const peer = FakePeerConnection.instances.at(-1);
    if (peer === undefined) throw new Error("No peer connection was created");
    return peer;
  }

  createDataChannel(label: string, init?: RTCDataChannelInit): RTCDataChannel {
    const channel = new FakeDataChannel(label, init);
    this.channels.push(channel);
    return channel as unknown as RTCDataChannel;
  }

  addTransceiver(
    trackOrKind: MediaStreamTrack | string,
    init: RTCRtpTransceiverInit = {},
  ): RTCRtpTransceiver {
    const track = typeof trackOrKind === "string" ? null : trackOrKind;
    const kind = typeof trackOrKind === "string" ? trackOrKind : trackOrKind.kind;
    const transceiver = this.transceiver(kind, init.direction ?? "sendrecv", track, init.streams ?? []);
    return transceiver as unknown as RTCRtpTransceiver;
  }

  addTrack(track: MediaStreamTrack, ...streams: MediaStream[]): RTCRtpSender {
    const transceiver = this.transceiver(track.kind, "sendrecv", track, streams);
    return transceiver.sender as unknown as RTCRtpSender;
  }

  removeTrack(sender: RTCRtpSender): void {
    const transceiver = this.transceivers.find((t) => (t.sender as unknown) === sender);
    if (transceiver === undefined) throw new Error("Unknown sender");
    transceiver.sender.track = null;
    transceiver.direction = transceiver.direction === "sendrecv" ? "recvonly" : "inactive";
  }

  getTransceivers(): RTCRtpTransceiver[] {
    return this.transceivers as unknown as RTCRtpTransceiver[];
  }

  getSenders(): RTCRtpSender[] {
    return this.transceivers.map((t) => t.sender) as unknown as RTCRtpSender[];
  }

  async createOffer(): Promise<RTCSessionDescriptionInit> {
    sdpCounter += 1;
    return { type: "offer", sdp: `offer-${sdpCounter}` };
  }

  async createAnswer(): Promise<RTCSessionDescriptionInit> {
    if (this.signalingState !== "have-remote-offer") throw new Error("No remote offer");
    sdpCounter += 1;
    return { type: "answer", sdp: `answer-${sdpCounter}` };
  }

  async setLocalDescription(description: RTCSessionDescriptionInit): Promise<void> {
    this.log.push(`local:${description.type}`);
    if (description.type === "rollback") {
      if (this.signalingState !== "have-local-offer") throw new Error("Nothing to roll back");
      this.localDescription = null;
      this.signalingState = "stable";
      return;
    }
    if (description.type === "offer") {
      if (this.signalingState !== "stable") throw new Error("Cannot offer now");
      this.signalingState = "have-local-offer";
    } else {
      if (this.signalingState !== "have-remote-offer") throw new Error("Cannot answer now");
      this.signalingState = "stable";
    }
    this.localDescription = description;
  }

  async setRemoteDescription(description: RTCSessionDescriptionInit): Promise<void> {
    this.log.push(`remote:${description.type}:${description.sdp}`);
    if (description.type === "offer") {
      if (this.signalingState !== "stable") throw new Error("Offer collision");
      this.signalingState = "have-remote-offer";
    } else {
      if (this.signalingState !== "have-local-offer") throw new Error("Unexpected answer");
      this.signalingState = "stable";
    }
    this.remoteDescription = description;
  }

  async addIceCandidate(candidate?: RTCIceCandidateInit): Promise<void> {
    if (this.remoteDescription === null) throw new Error("No remote description");
    this.candidates.push(candidate ?? null);
  }

  async getStats(): Promise<RTCStatsReport> {
    return new Map() as unknown as RTCStatsReport;
  }

  close(): void {
    this.closed = true;
    this.signalingState = "closed";
  }

  // ------------------------------------------------------------ test helpers

  changeConnectionState(state: RTCPeerConnectionState): void {
    this.connectionState = state;
    this.onconnectionstatechange?.();
  }

  /** Emit a local ICE candidate, as the browser would after setLocalDescription. */
  emitCandidate(candidate: RTCIceCandidateInit | null): void {
    this.onicecandidate?.({
      candidate: candidate === null ? null : { toJSON: () => candidate },
    } as RTCPeerConnectionIceEvent);
  }

  /** Add a remote transceiver as a server offer would. */
  remoteTransceiver(kind: "audio" | "video", direction: RTCRtpTransceiverDirection = "recvonly"): void {
    this.transceiver(kind, direction, null, []);
  }

  emitTrack(kind: "audio" | "video"): FakeTrack {
    const track = new FakeTrack(kind);
    const transceiver = this.transceivers.find((t) => t.receiver.track.kind === kind) ?? null;
    this.ontrack?.({
      track: track.asTrack(),
      streams: [],
      receiver: transceiver?.receiver,
      transceiver,
    } as unknown as RTCTrackEvent);
    return track;
  }

  remoteChannel(label: string): FakeDataChannel {
    const channel = new FakeDataChannel(label);
    this.ondatachannel?.({ channel } as unknown as RTCDataChannelEvent);
    return channel;
  }

  openChannels(): void {
    for (const channel of this.channels) channel.open();
  }

  private transceiver(
    kind: string,
    direction: RTCRtpTransceiverDirection,
    track: MediaStreamTrack | null,
    streams: readonly MediaStream[],
  ): FakeTransceiver {
    const transceiver: FakeTransceiver = {
      mid: String(this.transceivers.length),
      direction,
      currentDirection: null,
      receiver: { track: { kind } },
      sender: {
        track,
        streams: [...streams],
        async replaceTrack(next) {
          transceiver.sender.track = next;
        },
        setStreams(...next) {
          transceiver.sender.streams = next;
        },
      },
    };
    this.transceivers.push(transceiver);
    return transceiver;
  }
}

export function reset(): void {
  FakeWebSocket.instances = [];
  FakePeerConnection.instances = [];
}

/** Let queued promise callbacks and signaling operations run. */
export async function flush(rounds = 20): Promise<void> {
  for (let index = 0; index < rounds; index += 1) {
    await new Promise<void>((resolve) => setImmediate(resolve));
  }
}

export function hello(overrides: Json = {}): Json {
  return {
    type: "session",
    protocol: 1,
    session_id: "s-1",
    name: "practice",
    role: "answerer",
    ice_servers: [{ urls: ["stun:stun.example.org"], username: null, credential: null }],
    media: { audio: "send_receive", video: null, max_incoming_tracks: 4 },
    channels: [
      {
        name: "messages",
        id: 0,
        negotiated: true,
        ordered: true,
        max_retransmits: null,
        max_packet_life_time_ms: null,
        max_message_bytes: 64,
        receive_format: "json",
        send_format: "json",
      },
    ],
    limits: { negotiation_timeout_ms: 15000, max_signaling_message_bytes: 65536 },
    capabilities: { trickle_ice: false, ice_restart: false, renegotiation: true },
    ...overrides,
  };
}
