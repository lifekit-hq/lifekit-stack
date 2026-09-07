/**
 * contact-points.test.js — the Grafana dead-man template speaks the envelope
 * grammar (docs/notify-envelope.md).
 *
 * These alerts post to Telegram DIRECTLY, not through notify-relay, so a dead
 * relay cannot swallow the alarm that says devclaw is dead. That exemption costs
 * a duplicated ~15-line grammar in a Go template, and nothing else in the repo
 * executes Go templates — so this suite renders the template itself, through a
 * deliberately small evaluator for the subset it uses. The evaluator throws on
 * any construct it does not model, so an edit that outgrows it fails here rather
 * than being waved through untested.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { describe, it } from "node:test";

const ALERTING = new URL("./grafana/provisioning/alerting/", import.meta.url);
const tmpl = await readFile(new URL("contact-points.yml.tmpl", ALERTING), "utf8");
const rules = await readFile(new URL("rules.yml", ALERTING), "utf8");

/** The `message: |` block scalar, de-indented — i.e. what Grafana compiles. */
function messageTemplate(source) {
  const lines = source.split("\n");
  const start = lines.findIndex((line) => /^\s*message:\s*\|\s*$/.test(line));
  assert.notEqual(start, -1, "no `message: |` block in the contact point");
  const indent = lines[start + 1].length - lines[start + 1].trimStart().length;
  const body = [];
  for (const line of lines.slice(start + 1)) {
    if (line.trim() !== "" && line.length - line.trimStart().length < indent) break;
    body.push(line.slice(indent));
  }
  return body.join("\n");
}

function tokenize(source) {
  const tokens = [];
  const action = /\{\{(.*?)\}\}/gs;
  let cursor = 0;
  let match;
  while ((match = action.exec(source)) !== null) {
    if (match.index > cursor) {
      tokens.push({ kind: "text", value: source.slice(cursor, match.index) });
    }
    const expr = match[1].trim();
    if (expr.startsWith("-") || expr.endsWith("-")) {
      throw new Error(`whitespace-trim markers are not modelled: {{${match[1]}}}`);
    }
    tokens.push({ kind: "action", expr });
    cursor = match.index + match[0].length;
  }
  if (cursor < source.length) {
    tokens.push({ kind: "text", value: source.slice(cursor) });
  }
  return tokens;
}

const FIELD = /^\.(?:[A-Za-z][A-Za-z0-9_]*)(?:\.[A-Za-z][A-Za-z0-9_]*)*$/;

function parseBlock(tokens, start) {
  const nodes = [];
  let i = start;
  while (i < tokens.length) {
    const token = tokens[i];
    if (token.kind === "text") {
      nodes.push(token);
      i += 1;
      continue;
    }
    const { expr } = token;
    if (expr === "end" || expr === "else") return { nodes, next: i };
    if (expr.startsWith("range ")) {
      const path = expr.slice(6).trim();
      if (!FIELD.test(path)) throw new Error(`unsupported range: {{${expr}}}`);
      const body = parseBlock(tokens, i + 1);
      if (tokens[body.next]?.expr !== "end") throw new Error("unterminated range");
      nodes.push({ kind: "range", path, body: body.nodes });
      i = body.next + 1;
      continue;
    }
    if (expr.startsWith("if ")) {
      const cond = expr.slice(3).trim();
      const consequent = parseBlock(tokens, i + 1);
      let alternate = [];
      let next = consequent.next;
      if (tokens[next]?.expr === "else") {
        const other = parseBlock(tokens, next + 1);
        alternate = other.nodes;
        next = other.next;
      }
      if (tokens[next]?.expr !== "end") throw new Error("unterminated if");
      nodes.push({ kind: "if", cond, consequent: consequent.nodes, alternate });
      i = next + 1;
      continue;
    }
    if (!FIELD.test(expr)) throw new Error(`unsupported template action: {{${expr}}}`);
    nodes.push({ kind: "field", path: expr });
    i += 1;
  }
  return { nodes, next: i };
}

// A missing key on Grafana's Labels/Annotations (both map[string]string) renders
// as the zero value, not as an error — so an unset annotation is "".
function lookup(context, path) {
  return path
    .split(".")
    .filter(Boolean)
    .reduce((value, key) => (value == null ? undefined : value[key]), context);
}

const EQUALITY = /^eq\s+(\.\S+)\s+"([^"]*)"$/;

function truthy(context, cond) {
  const equality = EQUALITY.exec(cond);
  if (equality) return lookup(context, equality[1]) === equality[2];
  if (FIELD.test(cond)) {
    const value = lookup(context, cond);
    return value !== undefined && value !== null && value !== "";
  }
  throw new Error(`unsupported condition: {{if ${cond}}}`);
}

function evaluate(nodes, context) {
  let out = "";
  for (const node of nodes) {
    if (node.kind === "text") out += node.value;
    else if (node.kind === "field") out += lookup(context, node.path) ?? "";
    else if (node.kind === "if") {
      out += evaluate(
        truthy(context, node.cond) ? node.consequent : node.alternate,
        context,
      );
    } else {
      const items = lookup(context, node.path);
      assert.ok(Array.isArray(items), `${node.path} is not a list`);
      for (const item of items) out += evaluate(node.body, item);
    }
  }
  return out;
}

function compile(source) {
  const tokens = tokenize(source);
  const { nodes, next } = parseBlock(tokens, 0);
  // parseBlock stops at any `end`/`else`, so a stray one at top level would
  // otherwise drop every node after it and still render something plausible.
  if (next !== tokens.length) {
    throw new Error(`unbalanced {{${tokens[next].expr}}} at top level`);
  }
  return nodes;
}

const compiled = compile(messageTemplate(tmpl));
const render = (alerts) => evaluate(compiled, { Alerts: alerts }).trimEnd();

const alert = (Status, extra = {}) => ({
  Status,
  Labels: { alertname: "devclaw is down", severity: "critical" },
  Annotations: {
    summary: "devclaw-mcp is not answering /metrics",
    description: "Prometheus has not scraped devclaw-mcp for 3 minutes.",
    action: "docker compose -p devclaw up -d",
    ...extra,
  },
});

describe("the Grafana dead-man message", () => {
  it("sends HTML, since the grammar is markup", () => {
    assert.match(tmpl, /^\s*parse_mode: HTML$/m);
  });

  // The recovery notice is the point of rule 3: it must still be SENT, it just
  // must not repeat the firing body.
  it("keeps resolved notifications enabled", () => {
    assert.match(tmpl, /^\s*disableResolveMessage: false$/m);
  });

  it("opens with the envelope's line 1 and carries body then action", () => {
    assert.equal(
      render([alert("firing")]),
      "🔴 <b>grafana</b> · <b>devclaw is down</b> — devclaw-mcp is not answering /metrics\n" +
        "\n" +
        "Prometheus has not scraped devclaw-mcp for 3 minutes.\n" +
        "\n" +
        "→ <code>docker compose -p devclaw up -d</code>",
    );
  });

  // The bug this feature exists to fix: a RESOLVED message that repeats the
  // firing body ("the process is dead … docker ps on the box") is one you act on
  // at 3am. `good` renders line 1 and nothing else, however the rule is annotated.
  it("says only that it recovered — never the firing body or its remediation", () => {
    const out = render([alert("resolved")]);

    assert.equal(
      out,
      "🟢 <b>grafana</b> · <b>devclaw is down</b> — recovered",
    );
    assert.ok(!out.includes("Prometheus has not scraped"), out);
    assert.ok(!out.includes("docker compose"), out);
    assert.ok(!out.includes("<code>"), out);
  });

  // Unset annotations render as nothing — never as Go's `<no value>`, whose
  // angle brackets would make Telegram refuse the whole message.
  it("omits an optional block a rule did not annotate", () => {
    const noAction = render([alert("firing", { action: undefined })]);
    assert.ok(noAction.includes("Prometheus has not scraped"), noAction);
    assert.ok(!noAction.includes("→"), noAction);

    const bare = render([alert("firing", { description: undefined, action: undefined })]);
    assert.equal(
      bare,
      "🔴 <b>grafana</b> · <b>devclaw is down</b> — devclaw-mcp is not answering /metrics",
    );
  });

  // Grafana groups alerts into one notification; each one still has to stand
  // alone on a lock screen, and a resolved sibling must not inherit 🔴.
  it("renders one line 1 per alert in a grouped notification", () => {
    const out = render([alert("firing"), alert("resolved")]);

    assert.equal(out.match(/<b>grafana<\/b>/g).length, 2);
    assert.ok(out.includes("🔴 <b>grafana</b>"), out);
    assert.ok(out.includes("🟢 <b>grafana</b>"), out);
    // A blank line between them, so the second line 1 is not glued to the
    // first's action and read as part of it.
    assert.ok(
      out.includes(
        "</code>\n\n🟢 <b>grafana</b> · <b>devclaw is down</b> — recovered",
      ),
      out,
    );
  });
});

// Nothing escapes on this path: Grafana's Go template has no escaping function
// the Bot API is guaranteed to accept, and a mangled entity would break the one
// message that says devclaw is dead. What makes that safe is that every string
// rendered above is authored in rules.yml, in this repo — so it has to stay
// free of the three characters Telegram's HTML parser reads as markup.
// Enough of rules.yml to see each rule's title and annotations. The repo has no
// YAML parser it can use here (the relay image carries no npm dependencies), so
// this reads the two shapes the file uses — `key: value` and a folded `key: >-`
// block — by indentation, and asserts on any line it cannot place.
function parseRules(yaml) {
  const parsed = [];
  let current = null;
  let blockIndent = null; // indent of the `annotations:` key, while inside it
  let key = null;
  let keyIndent = Infinity;

  for (const line of yaml.split("\n")) {
    const indent = line.search(/\S/);
    if (line.trim() === "" || line.trim().startsWith("#")) {
      blockIndent = null;
      continue;
    }

    if (blockIndent !== null && indent > blockIndent) {
      if (key !== null && indent > keyIndent) {
        current.annotations[key] += ` ${line.trim()}`; // folded `>-` continuation
        continue;
      }
      const entry = /^\s+([a-z_]+):\s*(.*?)\s*$/.exec(line);
      assert.ok(entry, `unparsed annotation line: ${line}`);
      key = entry[1];
      keyIndent = indent;
      current.annotations[key] = entry[2] === ">-" || entry[2] === "|" ? "" : entry[2];
      continue;
    }
    blockIndent = null;
    key = null;

    const uid = /^\s+- uid:\s*(\S+)\s*$/.exec(line);
    if (uid) {
      current = { uid: uid[1], title: "", annotations: {} };
      parsed.push(current);
      continue;
    }
    if (!current) continue;
    const title = /^\s+title:\s*(\S.*?)\s*$/.exec(line);
    if (title) current.title = title[1];
    else if (/^\s+annotations:\s*$/.test(line)) blockIndent = indent;
  }
  return parsed;
}

describe("the strings the template renders unescaped", () => {
  it("reads every rule in the file", () => {
    const parsed = parseRules(rules);

    assert.equal(parsed.length, (rules.match(/^\s+- uid:/gm) ?? []).length);
    assert.ok(parsed.length >= 2, `found ${parsed.length} rules`);
  });

  // The template renders these four and nothing else, so an alert whose rule
  // leaves one unset arrives without a headline, body or remediation.
  it("gives every rule a title, summary, description and action", () => {
    for (const rule of parseRules(rules)) {
      assert.ok(rule.title, `${rule.uid} has no title`);
      for (const field of ["summary", "description", "action"]) {
        assert.ok(rule.annotations[field]?.trim(), `${rule.uid} has no ${field}`);
      }
    }
  });

  it("holds no HTML-special character in any of them", () => {
    for (const rule of parseRules(rules)) {
      const rendered = [
        rule.title,
        rule.annotations.summary,
        rule.annotations.description,
        rule.annotations.action,
      ];
      for (const value of rendered) {
        assert.doesNotMatch(value, /[<>&]/, `${rule.uid}: ${value} needs escaping`);
      }
    }
  });
});
