/**
 * The only thing that talks to the OpenClaw gateway. The model and `tool_choice: "none"` are fixed here and
 * the request carries no tools; nothing the panel sends reaches them. The agent answers as text (one JSON
 * object) and schema.js parses it. The door never sets x-openclaw-model (that needs operator.admin) and
 * holds the edge's own secret, not the gateway token.
 */

export const AGENT_MODEL = "openclaw/study-panel";

export class UpstreamError extends Error {
  constructor(reason, status) {
    super(reason);
    this.reason = reason;
    this.status = status;
  }
}

export function turnRequest(prompt, sessionKey) {
  return {
    model: AGENT_MODEL,
    user: sessionKey,
    stream: false,
    messages: [{ role: "user", content: prompt }],
    tool_choice: "none",
  };
}

/** Pulls the model's text out of a chat-completions answer: a finished turn with no tool call. */
export function replyText(answer) {
  const choice = answer?.choices?.[0];
  const message = choice?.message;
  if (choice?.finish_reason !== "stop" || message?.tool_calls?.length) throw new UpstreamError("no_text_reply");
  if (typeof message?.content !== "string") throw new UpstreamError("no_text_reply");
  return message.content;
}

export function createGateway({ baseUrl, secret, fetchImpl = fetch, timeoutMs = 90_000 }) {
  const headers = { authorization: `Bearer ${secret}`, "content-type": "application/json" };
  return {
    async turn(prompt, sessionKey, extra = {}) {
      let res;
      try {
        res = await fetchImpl(`${baseUrl}/v1/chat/completions`, {
          method: "POST",
          headers: { ...headers, ...extra },
          body: JSON.stringify(turnRequest(prompt, sessionKey)),
          signal: AbortSignal.timeout(timeoutMs),
        });
      } catch (err) {
        throw new UpstreamError(err?.name === "TimeoutError" ? "timeout" : "unreachable");
      }
      if (!res.ok) throw new UpstreamError("status", res.status);
      let answer;
      try {
        answer = await res.json();
      } catch {
        throw new UpstreamError("answer_not_json");
      }
      return replyText(answer);
    },
    /** Cheap authenticated probe: the models list answers only a caller the gateway accepts. */
    async probe() {
      let res;
      try {
        res = await fetchImpl(`${baseUrl}/v1/models`, { headers, signal: AbortSignal.timeout(5_000) });
      } catch {
        throw new UpstreamError("unreachable");
      }
      if (!res.ok) throw new UpstreamError("status", res.status);
    },
  };
}
