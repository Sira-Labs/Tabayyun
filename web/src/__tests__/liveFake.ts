// A stand-in for the browser's `EventSource` in the live event tests (spec 030).
import { vi } from "vitest";

type Listener = (event: MessageEvent) => void;

/** Records the streams the app opens and lets a test play the server. */
export class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static opened: FakeEventSource[] = [];

  readyState: number = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, Listener[]>();

  constructor(readonly url: string) {
    FakeEventSource.opened.push(this);
  }

  addEventListener(type: string, listener: Listener) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close() {
    this.readyState = FakeEventSource.CLOSED;
  }

  /** The server accepted the stream. */
  accept() {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.();
  }

  /** The server sent an event of this name. */
  emit(type: string, data: Record<string, unknown> = {}) {
    const event = new MessageEvent(type, { data: JSON.stringify({ type, ...data }) });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  /** The stream broke; the browser is connecting again. */
  drop() {
    this.readyState = FakeEventSource.CONNECTING;
    this.onerror?.();
  }

  /** The server refused the stream; the browser gives up on it. */
  refuse() {
    this.readyState = FakeEventSource.CLOSED;
    this.onerror?.();
  }
}

/** Install the fake as `EventSource` until the test ends; returns the streams opened so far. */
export function fakeEventSource(): FakeEventSource[] {
  FakeEventSource.opened = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  return FakeEventSource.opened;
}
