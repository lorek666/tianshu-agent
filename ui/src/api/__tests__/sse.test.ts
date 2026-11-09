import { describe, expect, it } from "vitest";
import { createSseParser } from "../sse";
import type { SseFrame } from "../sse";

describe("SSE 解析", () => {
  const parse = (chunks: string[]): SseFrame[] => {
    const frames: SseFrame[] = [];
    const parser = createSseParser((frame) => frames.push(frame));
    for (const chunk of chunks) parser.push(chunk);
    parser.flush();
    return frames;
  };

  it("解析 event + data，并支持任意分块", () => {
    const frames = parse(['event: ev', 'ent\ndata: {"seq":1}\n\n']);
    expect(frames).toEqual([{ event: "event", data: '{"seq":1}' }]);
  });

  it("支持 CRLF、多行 data 与注释行", () => {
    const frames = parse([": keep-alive\r\n\r\ndata: line1\r\ndata: line2\r\n\r\n"]);
    expect(frames).toEqual([{ event: "message", data: "line1\nline2" }]);
  });

  it("忽略没有 data 的帧，flush 吐出没有以空行结尾的最后一帧", () => {
    const frames = parse(["event: ping\n\n", 'event: event\ndata: {"seq":2}']);
    expect(frames).toEqual([{ event: "event", data: '{"seq":2}' }]);
  });
});