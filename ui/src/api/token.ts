/** 读访问令牌 / 判断演示模式。二者都不把令牌写进任何持久化位置。 */

export const TOKEN_META_NAME = "tianshu-token";

/** 从 Host 注入的 `<meta name="tianshu-token">` 读取令牌；读不到返回 null。 */
export function readHostToken(doc: Document | null = typeof document === "undefined" ? null : document): string | null {
  if (doc === null) return null;
  const meta = doc.querySelector(`meta[name="${TOKEN_META_NAME}"]`);
  const content = meta?.getAttribute("content")?.trim() ?? "";
  return content === "" ? null : content;
}

/**
 * 静态预览模式：`?demo=1`。
 *
 * 只用于“没有后端时看界面长什么样”，数据全部来自打包进产物的演示夹具，
 * 不发出任何网络请求。真实 Host 加载时不带这个参数，走真实 RPC。
 * 令牌永远不会通过 URL 传递——演示模式根本不需要令牌。
 */
export function isDemoRequested(loc: Location | null = typeof location === "undefined" ? null : location): boolean {
  if (loc === null) return false;
  try {
    const params = new URLSearchParams(loc.search);
    if (!params.has("demo")) return false;
    const value = (params.get("demo") ?? "").trim().toLowerCase();
    return value === "" || ["1", "true", "yes", "preview", "demo"].includes(value);
  } catch {
    return false;
  }
}