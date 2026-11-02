export type StreamEvent = { event: string; data: Record<string, unknown> };

export async function* parseSse(response: Response): AsyncGenerator<StreamEvent> {
  if (!response.ok || !response.body) {
    const payload = await response.json().catch(() => null) as { error?: { message?: string } } | null;
    throw new Error(payload?.error?.message ?? "智能客服暂时不可用");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lines: string[] = [];
  let finished = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      while (true) {
        const boundary = buffer.search(/[\r\n]/);
        if (boundary < 0) break;
        if (buffer[boundary] === "\r" && boundary === buffer.length - 1 && !done) break;
        const line = buffer.slice(0, boundary);
        const width = buffer[boundary] === "\r" && buffer[boundary + 1] === "\n" ? 2 : 1;
        buffer = buffer.slice(boundary + width);
        if (line !== "") {
          lines.push(line);
          continue;
        }
        const event = lines.find((item) => item.startsWith("event:"))?.slice(6).trim();
        const data = lines.filter((item) => item.startsWith("data:")).map((item) => item.slice(5).trim()).join("\n");
        lines = [];
        if (event && data) yield { event, data: JSON.parse(data) as Record<string, unknown> };
      }
      if (done) { finished = true; break; }
    }
  } finally {
    if (!finished) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
