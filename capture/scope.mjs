/** Reject manual trade routes before automatic navigation or body access.
 * 自动导航或读取响应正文前，拒绝人工贸易数据通道。 */
export function isAutoCaptureExcludedUrl(rawUrl) {
  try {
    const url = new URL(rawUrl, "https://crm.xiaoman.cn");
    const pathname = url.pathname.toLowerCase().replace(/\/+$/, "") || "/";
    return (pathname === "/crm/customer/personal" && url.searchParams.get("tab")?.toLowerCase() === "seadata")
      || pathname === "/ciq/info" || pathname.startsWith("/ciq/info/")
      || pathname === "/api/ciqread" || pathname.startsWith("/api/ciqread/")
      || pathname === "/api/aiwordread/wordcloud" || pathname.startsWith("/api/aiwordread/wordcloud/");
  } catch { return false; }
}

export function assertAutomaticCaptureUrl(rawUrl) {
  if (isAutoCaptureExcludedUrl(rawUrl)) throw new Error("manual trade route excluded from automatic capture");
}
