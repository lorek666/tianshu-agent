/**
 * 最小 SSE 解析器。
 *
 * 不用浏览器原生 `EventSource`，因为它**不能带自定义请求头**，而 M5 契约要求
 * 每个请求都带 `X-Tianshu-Token`。这里用 fetch 流 + 手写解析，重连语义由
 * `api/stream.ts` 实现（带 `since_seq`，与 EventSource 等价）。
 *
 * 只实现 Host 实际会发的子集：`event:` / `data:` / 注释行 / 多行 data / CRLF。
 */

export interface SseFrame {
  event: string;
  data: string;
}

export interface SseParser {
  push(chunk: string): void;
  flush(): void;
}

export function createSseParser(onFrame: (frame: SseFrame) => void): SseParser {
  let buffer = "";

  const dispatch = (block: string): void => {
    let eventName = "message";
    const dataLines: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line === "" || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") eventName = value;
      else if (field === "data") dataLines.push(value);
    }
    if (dataLines.length > 0) onFrame({ event: eventName, data: dataLines.join("\n") });
  };

  return {
    push(chunk: string): void {
      buffer += chunk;
      let match = /\r?\n\r?\n/.exec(buffer);
      while (match !== null) {
        const index = match.index;
        const block = buffer.slice(0, index);
        buffer = buffer.slice(index + match[0].length);
        dispatch(block);
        match = /\r?\n\r?\n/.exec(buffer);
      }
    },
    flush(): void {
      if (buffer.trim() !== "") {
        dispatch(buffer);
        buffer = "";
      }
    },
  };
}