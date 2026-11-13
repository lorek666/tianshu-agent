import { createServer } from "node:http";
import { afterEach, describe, expect, it } from "vitest";
import { createRealHostClient } from "../host";
import { HostRpcError } from "../errors";

// Node HTTP 夹具使用 JavaScript；浏览器包无需新增 Node 类型依赖。
const servers = [];

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => new Promise((resolve) => {
    server.closeAllConnections();
    server.close(() => resolve());
  })));
});

async function localHost(reply) {
  const server = createServer((_request, response) => reply(response));
  servers.push(server);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("No test address");
  return `http://127.0.0.1:${address.port}`;
}

describe("真实 Host HTTP 响应读取", () => {
  it("响应头已到但正文停滞时仍按请求期限报 timeout", async () => {
    let headersSent = false;
    const origin = await localHost((response) => {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.write('{"ok":true,"result":');
      headersSent = true;
      // 保留未完成正文，直到客户端取消或测试清理；不以 sleep 猜测响应顺序。
    });
    const host = createRealHostClient("fixture-only", origin);
    const result = host.rpc("state", {}, { timeoutMs: 200 }).catch((error) => error);
    let guard;
    try {
      const settled = await Promise.race([
        result,
        new Promise((resolve) => {
          guard = setTimeout(() => resolve("body-still-pending"), 2_000);
        }),
      ]);
      expect(headersSent).toBe(true);
      expect(settled).toBeInstanceOf(HostRpcError);
      expect(settled.code).toBe("timeout");
    } finally {
      clearTimeout(guard);
      servers[0]?.closeAllConnections();
      await result;
    }
  });

  it("完整响应保持原结果", async () => {
    const origin = await localHost((response) => {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ ok: true, result: { resumed: true } }));
    });
    await expect(createRealHostClient("fixture-only", origin).rpc("run.resume"))
      .resolves.toEqual({ resumed: true });
  });

  it("HTTP 错误保留服务端分类", async () => {
    const origin = await localHost((response) => {
      response.writeHead(409, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ ok: false, error: { code: "conflict", message: "stale" } }));
    });
    await expect(createRealHostClient("fixture-only", origin).rpc("approvals.decide"))
      .rejects.toMatchObject({ code: "conflict", httpStatus: 409 });
  });
});
