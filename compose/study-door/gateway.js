/**
 * The only thing that talks to the OpenClaw gateway. The model, the one tool and the pinned tool_choice
 * are fixed here; nothing the panel sends reaches them. The door never sets x-openclaw-model (that needs
 * operator.admin) and holds the edge's own secret, not the gateway token.
 */

import { REPLY_TOOL } from "./schema.js";

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
    tools: [REPLY_TOOL],
    tool_choice: { type: "function", function: { name: "reply" } },
  };
}

/** Pulls the reply call's arguments out of a chat-completions answer. */
export function replyArguments(answer) {
  const choice = answer?.choices?.[0];
  const calls = choice?.message?.tool_calls;
  if (choice?.finish_reason !== "tool_calls" || !Array.isArray(calls) || calls.length !== 1) {
    throw new UpstreamError("no_tool_call");
  }
  const fn = calls[0]?.function;
  if (fn?.name !== "reply" || typeof fn.arguments !== "string") throw new UpstreamError("wrong_tool");
  try {
    return JSON.parse(fn.arguments);
  } catch {
    throw new UpstreamError("arguments_not_json");
  }
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
      return replyArguments(answer);
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
